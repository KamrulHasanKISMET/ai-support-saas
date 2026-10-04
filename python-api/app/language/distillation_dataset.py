"""
DISTILLATION DATASET BUILDER -- Phase 6.

Turns verified language_experiences rows into a leak-free, reproducible
train / validation / test split for the own intent model.

Rules (each one is tested):
  * Only rows that clear the SAME evidence gate ClusterBuilderService
    uses (§2.3) are fetched -- the LLM's label alone is never ground
    truth. Rows must also be control_plane.is_training_eligible().
  * TENANT ISOLATION (§4): a dataset is built for exactly one tenant;
    every row must carry that tenant_id or the build refuses.
  * Exact duplicates (case/space-insensitive) are collapsed to one row,
    so the same sentence can never sit in two splits.
  * Any text that also appears in the held-out generalization eval set
    is DROPPED from training (leakage guard) -- the eval set must stay
    a fair test of phrasing the model never saw.
  * The split is a deterministic hash of (tenant, experience_id): the
    same data always gives the same split; adding new rows never moves
    an old row between splits.
  * Classes with too few examples are excluded and REPORTED, not
    silently trained on.
  * The dataset fingerprint (hash of ids + splits, NOT text) goes into
    the model registry, so a model version says exactly what it saw.

Pure functions + one thin async fetch. No embedding, no model here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from app.language import control_plane

MIN_PER_CLASS_TOTAL = 12   # below this a class cannot be split 70/15/15 meaningfully
SPLIT_TRAIN, SPLIT_VAL, SPLIT_TEST = 0.70, 0.15, 0.15
EVAL_SET_PATH = Path(__file__).parent / "eval_sets" / "multilingual_intents_v1.json"

# Same gate as cluster_builder_service._fetch_eligible_rows (kept in sync by a test).
ELIGIBLE_ROWS_SQL = """
    SELECT le.experience_id, le.final_intent, le.normalized_message,
           le.language, le.verification_level
      FROM language_experiences le
      LEFT JOIN tenant_calibration_config tcc ON tcc.tenant_id = le.tenant_id
     WHERE le.tenant_id          = :tenant_id
       AND le.learning_eligible  = TRUE
       AND le.normalized_message IS NOT NULL
       AND le.final_intent       IS NOT NULL
       AND le.superseded_by      IS NULL
       AND (
            le.verification_level IN ('outcome_positive', 'human_confirmed')
            OR (le.verification_level = 'self_consistent'
                AND le.source_reliability >= COALESCE(tcc.min_reliability, 0.90))
       )
     ORDER BY le.created_at ASC
"""


@dataclass(frozen=True)
class Example:
    experience_id: str
    intent: str
    text: str
    language: str = "und"
    tenant_id: int | None = None


@dataclass
class Dataset:
    tenant_id: int
    train: list[Example] = field(default_factory=list)
    val: list[Example] = field(default_factory=list)
    test: list[Example] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)   # reason -> count
    excluded_classes: dict[str, int] = field(default_factory=dict)  # intent -> n
    fingerprint: str = ""

    def counts(self) -> dict:
        return {"train": len(self.train), "val": len(self.val), "test": len(self.test)}

    def classes(self) -> list[str]:
        return sorted({e.intent for e in self.train})


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def load_eval_texts(path: Path = EVAL_SET_PATH) -> set[str]:
    """Normalised texts of the held-out eval set (for the leakage guard)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {_norm(c["text"]) for c in data.get("cases", []) if c.get("text")}


def split_of(tenant_id: int, experience_id: str) -> str:
    """Deterministic split. Stable under insertion of other rows."""
    h = hashlib.sha256(f"split:{tenant_id}:{experience_id}".encode()).digest()
    u = int.from_bytes(h[:8], "big") / 2**64
    if u < SPLIT_TRAIN:
        return "train"
    if u < SPLIT_TRAIN + SPLIT_VAL:
        return "val"
    return "test"


def build_dataset(
    tenant_id: int,
    rows: Iterable[Example],
    *,
    eval_texts: set[str] | None = None,
    min_per_class: int = MIN_PER_CLASS_TOTAL,
) -> Dataset:
    """Pure. Raises ValueError on a cross-tenant row (loud by design)."""
    eval_texts = load_eval_texts() if eval_texts is None else eval_texts
    ds = Dataset(tenant_id=tenant_id)

    def drop(reason: str) -> None:
        ds.excluded[reason] = ds.excluded.get(reason, 0) + 1

    seen: set[str] = set()
    kept: list[Example] = []
    for r in rows:
        if r.tenant_id is not None and r.tenant_id != tenant_id:
            raise ValueError(
                f"cross-tenant row refused: row tenant {r.tenant_id} != dataset tenant {tenant_id}"
            )
        if not r.intent or not (r.text or "").strip():
            drop("empty"); continue
        if not control_plane.is_training_eligible(r.intent):
            drop("training_ineligible"); continue
        key = _norm(r.text)
        if key in eval_texts:
            drop("eval_set_leak"); continue
        if key in seen:
            drop("duplicate"); continue
        seen.add(key)
        kept.append(r)

    per_class: dict[str, int] = {}
    for r in kept:
        per_class[r.intent] = per_class.get(r.intent, 0) + 1
    ds.excluded_classes = {i: n for i, n in per_class.items() if n < min_per_class}

    for r in kept:
        if r.intent in ds.excluded_classes:
            continue
        getattr(ds, split_of(tenant_id, r.experience_id)).append(r)

    ident = sorted((e.experience_id, s) for s, part in
                   (("train", ds.train), ("val", ds.val), ("test", ds.test)) for e in part)
    ds.fingerprint = hashlib.sha256(json.dumps(ident).encode()).hexdigest()
    return ds


async def fetch_examples(db, tenant_id: int) -> list[Example]:
    """Thin DB read using the same evidence gate as cluster building."""
    from sqlalchemy import text  # lazy: keeps this module importable without a DB stack

    result = await db.execute(text(ELIGIBLE_ROWS_SQL), {"tenant_id": tenant_id})
    return [
        Example(
            experience_id=str(r.experience_id),
            intent=r.final_intent,
            text=r.normalized_message,
            language=(r.language or "und"),
            tenant_id=tenant_id,
        )
        for r in result
    ]
