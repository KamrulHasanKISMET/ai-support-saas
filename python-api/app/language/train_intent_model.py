"""
TRAIN THE OWN INTENT MODEL -- Phase 6 job (out-of-band; not on the
request path).

    python -m app.language.train_intent_model --tenant N [--dry-run] [--json]

Pipeline (each step is a small, tested function):
  fetch verified examples -> build leak-free dataset -> embed ->
  train -> calibrate temperature on val -> evaluate on test vs the
  nearest-centroid baseline -> register as 'trained' (a failing model is
  registered too, with ok=false and its failures, so the attempt is
  auditable; it can never enter shadow).

What a successful run DOES NOT do: it does not serve, route or change
any reply. The model waits at 'trained' until a human moves it to
'shadow' (model_registry.set_status). It also does not run without real
data: with too little verified data it stops and says exactly why.

The embedding provider and DB are injected (`embed_fn`, `db`) so the
whole flow is unit-tested with fakes. Real quality is unknown until
this is run on real traffic (PENDING_WORK.md A5).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass

import numpy as np

from app.core.logging import logger
from app.language import distillation_dataset as dd
from app.language import model_eval, model_registry
from app.language.intent_model import IntentModel, ModelError

MIN_TRAIN_EXAMPLES = 60      # below this, refuse: nothing meaningful can be learned
MIN_CLASSES = 2
EMBED_BATCH = 96


@dataclass
class TrainOutcome:
    status: str                     # 'registered' | 'dry_run' | 'insufficient_data' | 'error'
    detail: str = ""
    version: str | None = None
    report: dict | None = None
    counts: dict | None = None
    excluded: dict | None = None
    excluded_classes: dict | None = None


async def _embed_all(embed_fn, texts: list[str]) -> np.ndarray:
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        out.extend(await embed_fn(texts[i:i + EMBED_BATCH]))
    return np.asarray(out, dtype=np.float64)


async def train_for_tenant(
    db, tenant_id: int, *, embed_fn, embedding_model: str,
    dry_run: bool = False, eval_texts: set[str] | None = None,
) -> TrainOutcome:
    """Never raises: every failure becomes a TrainOutcome the caller prints."""
    try:
        examples = await dd.fetch_examples(db, tenant_id)
        ds = dd.build_dataset(tenant_id, examples, eval_texts=eval_texts)
        counts = ds.counts()
        common = dict(counts=counts, excluded=ds.excluded, excluded_classes=ds.excluded_classes)
        if len(ds.train) < MIN_TRAIN_EXAMPLES or len(ds.classes()) < MIN_CLASSES:
            return TrainOutcome(
                "insufficient_data",
                f"train={len(ds.train)} (need {MIN_TRAIN_EXAMPLES}), classes={len(ds.classes())} "
                f"(need {MIN_CLASSES}); verified data has not accumulated yet", **common)

        Xtr = await _embed_all(embed_fn, [e.text for e in ds.train])
        Xva = await _embed_all(embed_fn, [e.text for e in ds.val]) if ds.val else np.zeros((0, Xtr.shape[1]))
        Xte = await _embed_all(embed_fn, [e.text for e in ds.test]) if ds.test else np.zeros((0, Xtr.shape[1]))
        ytr = [e.intent for e in ds.train]

        model = IntentModel().fit(Xtr, ytr)
        if len(ds.val) >= 2:
            model.calibrate_temperature(Xva, [e.intent for e in ds.val])
        report = model_eval.evaluate(
            model, Xte, [e.intent for e in ds.test], [e.language for e in ds.test],
            Xtr=Xtr, ytr=ytr,
        )
        rd = model_eval.report_to_dict(report)
        if dry_run:
            return TrainOutcome("dry_run", "evaluated, nothing written", report=rd, **common)
        version = await model_registry.register_model(
            db, tenant_id=tenant_id, model=model, embedding_model=embedding_model,
            dataset_fingerprint=ds.fingerprint, train_counts=counts, eval_report=rd,
        )
        return TrainOutcome("registered", "ok" if rd["ok"] else "registered but FAILED the gate: " + "; ".join(rd["failures"]),
                            version=version, report=rd, **common)
    except (ModelError, ValueError) as exc:
        return TrainOutcome("error", f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 -- job boundary
        logger.error("train_intent_model failed tenant=%s", tenant_id, exc_info=True)
        return TrainOutcome("error", f"unexpected {type(exc).__name__}: {exc}")


async def _main(args) -> int:  # pragma: no cover -- needs real DB + provider
    from app.ai.embedding_service import embedding_service
    from app.core.database import AsyncSessionLocal
    from app.language.scheduler_lock import advisory_job_lock

    async with advisory_job_lock(f"train_intent_model:{args.tenant}") as acquired:
        if not acquired:
            print("skipped: another training run holds the lock")
            return 0
        async with AsyncSessionLocal() as db:
            out = await train_for_tenant(
                db, args.tenant, embed_fn=embedding_service.embed_batch,
                embedding_model=embedding_service.model, dry_run=args.dry_run)
    print(json.dumps(out.__dict__, indent=2, default=str) if args.json
          else f"{out.status}: {out.detail}\ncounts={out.counts} excluded={out.excluded}")
    return 0 if out.status in ("registered", "dry_run") else 1


if __name__ == "__main__":  # pragma: no cover
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    sys.exit(asyncio.run(_main(ap.parse_args())))
