"""
DEEP-LANGUAGE PHENOMENA EVAL -- Phase 5/6 (P5-5 / P6T-3,
docs/OWN_LANGUAGE_MODEL_TRAINING_PLAN.md §2, §6).

Pure grading over `eval_sets/phenomena_v1.json`: given a mapping of
case id -> predicted intent (and, for L5 pairs, optionally a
"declined"/"low_confidence" marker), produces per-layer accuracy and a
pair-consistency check for the layers where getting each case
individually "close" is not the point -- the point is that the SAME
surface pattern must be handled differently under different context or
negation (L5, L7).

This module does not run a model. It is called by whoever evaluates a
candidate (offline eval script, a notebook, a test) by first predicting
each case's `text` (+ `context` if present) with that candidate.

Design notes:
  * L5 "ambiguous-without-context" pairs (see phenomena_v1.json case
    l5_ctx_size...b) are graded more forgivingly: DECLINED (the
    candidate says "unclear"/asks to clarify) counts as a PASS for that
    one case, on the theory that confidently reusing the other pair
    member's answer is the actual failure mode being tested, not
    "picking the wrong specific label". A case opts into this by setting
    "notes" containing the literal words "CONSISTENCY check" (already
    true for that case) -- there is no separate schema flag to avoid two
    sources of truth; a test pins the current case to this behaviour.
  * `score_pairs` fails a pair when the two predictions are IDENTICAL
    despite different expected intents (the exact bug this layer
    targets: reusing one member's answer for the other unchanged).
  * Never raises on a missing prediction: it counts as wrong / absent,
    reported separately from actually-wrong so "candidate did not
    attempt this case" is visible.

Pure module: stdlib only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

EVAL_SET_PATH = Path(__file__).parent / "eval_sets" / "phenomena_v1.json"
DECLINE_MARKERS = frozenset({"declined", "low_confidence", "unclear", "clarify"})


@dataclass(frozen=True)
class Case:
    id: str
    layer: str
    intent: str
    text: str
    language: str
    pair_id: str | None
    concept_id: str | None = None
    context: list | None = None
    notes: str = ""

    @property
    def is_consistency_only(self) -> bool:
        return "consistency check" in self.notes.lower()


@dataclass
class LayerReport:
    layer: str
    n: int
    correct: int
    missing: int

    @property
    def accuracy(self) -> float:
        return (self.correct / self.n) if self.n else 0.0


@dataclass
class PairResult:
    pair_id: str
    ok: bool
    reason: str


def load_cases(path: Path = EVAL_SET_PATH) -> list[Case]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        Case(
            id=c["id"], layer=c["layer"], intent=c["intent"], text=c["text"],
            language=c.get("language", "und"), pair_id=c.get("pair_id"),
            concept_id=c.get("concept_id"), context=c.get("context"),
            notes=c.get("notes", ""),
        )
        for c in data["cases"]
    ]


def _is_pass(case: Case, predicted: str | None) -> bool:
    if predicted is None:
        return False
    if case.is_consistency_only and predicted.lower() in DECLINE_MARKERS:
        return True
    return predicted == case.intent


def score(cases: list[Case], predictions: dict[str, str]) -> dict[str, LayerReport]:
    """predictions: case.id -> predicted intent (or a decline marker).
    Missing ids are NOT KeyErrors -- they count as unattempted."""
    by_layer: dict[str, LayerReport] = {}
    for c in cases:
        r = by_layer.setdefault(c.layer, LayerReport(c.layer, 0, 0, 0))
        r.n += 1
        pred = predictions.get(c.id)
        if pred is None:
            r.missing += 1
        elif _is_pass(c, pred):
            r.correct += 1
    return by_layer


def score_pairs(cases: list[Case], predictions: dict[str, str]) -> list[PairResult]:
    """One result per pair_id. A pair PASSES only if every member passes
    _is_pass. Note this already implies the two predictions differ
    whenever the two expected intents differ (a model that reuses one
    member's literal label for both cannot pass both `_is_pass` checks
    at once unless it also correctly declines a consistency-only member)
    -- so "reused the same answer for a different context/negation" is
    caught as an individual failure on whichever member got the wrong
    label, and is reported as such rather than as a separate category."""
    groups: dict[str, list[Case]] = {}
    for c in cases:
        if c.pair_id:
            groups.setdefault(c.pair_id, []).append(c)

    out: list[PairResult] = []
    for pid, members in groups.items():
        preds = [predictions.get(m.id) for m in members]
        individually_ok = all(_is_pass(m, p) for m, p in zip(members, preds))
        out.append(PairResult(pid, individually_ok,
                               "ok" if individually_ok else "one or more members failed "
                               "(reusing the other member's answer under different context/negation "
                               "is the typical way this fails)"))
    return out


def report(cases: list[Case], predictions: dict[str, str]) -> dict:
    layers = score(cases, predictions)
    pairs = score_pairs(cases, predictions)
    total_n = sum(r.n for r in layers.values())
    total_correct = sum(r.correct for r in layers.values())
    return {
        "overall_accuracy": round(total_correct / total_n, 4) if total_n else 0.0,
        "n": total_n,
        "per_layer": {l: {"n": r.n, "correct": r.correct, "missing": r.missing,
                           "accuracy": round(r.accuracy, 4)} for l, r in sorted(layers.items())},
        "pairs": {p.pair_id: {"ok": p.ok, "reason": p.reason} for p in pairs},
        "pair_pass_rate": round(sum(p.ok for p in pairs) / len(pairs), 4) if pairs else None,
        "reviewed_by_native_speakers": json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))["reviewed_by_native_speakers"],
    }
