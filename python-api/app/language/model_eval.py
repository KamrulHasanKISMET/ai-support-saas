"""
OFFLINE EVALUATION for the own intent model -- Phase 6.

Scores a trained IntentModel on a held-out split and compares it with a
BASELINE (nearest-centroid on the same embeddings -- the same idea the
cluster matcher uses). A trained model is only worth the added
complexity if it beats that baseline; this module makes the comparison
mechanical.

Reports (all computed from predictions only -- no text is read):
  accuracy, macro-F1, per-language accuracy (slices), expected
  calibration error (ECE), and selective accuracy at a confidence
  threshold (accuracy on the turns the model would ANSWER, plus
  coverage = share of turns it would answer). Coverage x accuracy is
  the commercial number: how much LLM work could it take over, at what
  error rate.

passes_gate() is a starting-point rule with THRESHOLDS_CALIBRATED=False,
the same honesty flag generalization_eval.py uses: the numbers are
placeholders until a real run justifies them. A model that passes is
only eligible to enter SHADOW (model_registry.py) -- never to answer.

Pure module: numpy + stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

THRESHOLDS_CALIBRATED = False          # flip only via a real, logged calibration
MIN_TEST_EXAMPLES = 60                 # below this the numbers are not evidence
MIN_ACCURACY = 0.85
MIN_GAIN_OVER_BASELINE = 0.02          # must beat nearest-centroid by 2 points
MAX_ECE = 0.10
MIN_LANGUAGE_SLICE_N = 10              # slices smaller than this are reported, not gated
MIN_LANGUAGE_ACCURACY = 0.70
ANSWER_THRESHOLD = 0.85                # confidence at/above which the model "would answer"
MIN_SELECTIVE_ACCURACY = 0.95          # accuracy on the turns it would answer


@dataclass
class EvalReport:
    n: int
    accuracy: float
    macro_f1: float
    ece: float
    coverage: float                    # share of turns with confidence >= ANSWER_THRESHOLD
    selective_accuracy: float | None   # None if it would answer nothing
    baseline_accuracy: float
    per_language: dict[str, dict] = field(default_factory=dict)
    per_intent_f1: dict[str, float] = field(default_factory=dict)
    ok: bool = False
    failures: list[str] = field(default_factory=list)
    thresholds_calibrated: bool = THRESHOLDS_CALIBRATED


def nearest_centroid_predict(Xtr, ytr: list[str], Xte) -> list[str]:
    """Baseline: cosine to the per-class mean embedding."""
    Xtr, Xte = np.asarray(Xtr, float), np.asarray(Xte, float)

    def nrm(A):
        n = np.linalg.norm(A, axis=1, keepdims=True)
        n[n == 0] = 1
        return A / n

    Xtr, Xte = nrm(Xtr), nrm(Xte)
    labels = sorted(set(ytr))
    C = nrm(np.stack([Xtr[[i for i, l in enumerate(ytr) if l == lab]].mean(axis=0) for lab in labels]))
    return [labels[i] for i in (Xte @ C.T).argmax(axis=1)]


def _macro_f1(y_true: list[str], y_pred: list[str]) -> tuple[float, dict[str, float]]:
    per: dict[str, float] = {}
    for lab in sorted(set(y_true)):
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == lab and p == lab)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != lab and p == lab)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == lab and p != lab)
        denom = 2 * tp + fp + fn
        per[lab] = (2 * tp / denom) if denom else 0.0
    return (sum(per.values()) / len(per) if per else 0.0), per


def expected_calibration_error(conf: list[float], correct: list[bool], bins: int = 10) -> float:
    if not conf:
        return 0.0
    c, k = np.asarray(conf), np.asarray(correct, float)
    edges = np.linspace(0, 1, bins + 1)
    ece = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (c > lo) & (c <= hi) if lo > 0 else (c >= lo) & (c <= hi)
        if m.any():
            ece += m.mean() * abs(k[m].mean() - c[m].mean())
    return float(ece)


def evaluate(model, Xte, yte: list[str], langs: list[str], *, Xtr, ytr: list[str]) -> EvalReport:
    """Score `model` on the test split; compare to nearest-centroid
    trained on the same train split. Test labels the model never saw
    count as errors (a real model would get them wrong)."""
    if len(yte) != len(langs) or len(yte) != len(Xte):
        raise ValueError("Xte, yte and langs must have the same length")
    preds = model.predict(Xte)
    y_pred = [p for p, _ in preds]
    conf = [c for _, c in preds]
    correct = [t == p for t, p in zip(yte, y_pred)]
    n = len(yte)
    acc = sum(correct) / n if n else 0.0
    f1, per_intent = _macro_f1(yte, y_pred)
    base_pred = nearest_centroid_predict(Xtr, ytr, Xte) if n else []
    base_acc = (sum(1 for t, p in zip(yte, base_pred) if t == p) / n) if n else 0.0

    answered = [i for i, c in enumerate(conf) if c >= ANSWER_THRESHOLD]
    sel = (sum(correct[i] for i in answered) / len(answered)) if answered else None

    per_lang: dict[str, dict] = {}
    for lg in sorted(set(langs)):
        idx = [i for i, l in enumerate(langs) if l == lg]
        per_lang[lg] = {"n": len(idx), "accuracy": sum(correct[i] for i in idx) / len(idx)}

    rep = EvalReport(
        n=n, accuracy=acc, macro_f1=f1,
        ece=expected_calibration_error(conf, correct),
        coverage=(len(answered) / n) if n else 0.0,
        selective_accuracy=sel, baseline_accuracy=base_acc,
        per_language=per_lang, per_intent_f1=per_intent,
    )
    rep.failures = gate_failures(rep)
    rep.ok = not rep.failures
    return rep


def gate_failures(r: EvalReport) -> list[str]:
    """Empty list == passes. Each string names exactly one failed rule."""
    f: list[str] = []
    if r.n < MIN_TEST_EXAMPLES:
        f.append(f"test_set_too_small:{r.n}<{MIN_TEST_EXAMPLES}")
        return f   # nothing else is meaningful on a tiny set
    if r.accuracy < MIN_ACCURACY:
        f.append(f"accuracy:{r.accuracy:.3f}<{MIN_ACCURACY}")
    if r.accuracy < r.baseline_accuracy + MIN_GAIN_OVER_BASELINE:
        f.append(f"not_better_than_baseline:{r.accuracy:.3f} vs {r.baseline_accuracy:.3f}")
    if r.ece > MAX_ECE:
        f.append(f"calibration:ece {r.ece:.3f}>{MAX_ECE}")
    if r.selective_accuracy is not None and r.selective_accuracy < MIN_SELECTIVE_ACCURACY:
        f.append(f"selective_accuracy:{r.selective_accuracy:.3f}<{MIN_SELECTIVE_ACCURACY}")
    for lg, s in r.per_language.items():
        if s["n"] >= MIN_LANGUAGE_SLICE_N and s["accuracy"] < MIN_LANGUAGE_ACCURACY:
            f.append(f"language_slice:{lg} {s['accuracy']:.3f}<{MIN_LANGUAGE_ACCURACY}")
    return f


def report_to_dict(r: EvalReport) -> dict:
    return {
        "n": r.n, "accuracy": round(r.accuracy, 4), "macro_f1": round(r.macro_f1, 4),
        "ece": round(r.ece, 4), "coverage": round(r.coverage, 4),
        "selective_accuracy": None if r.selective_accuracy is None else round(r.selective_accuracy, 4),
        "baseline_accuracy": round(r.baseline_accuracy, 4),
        "per_language": r.per_language, "per_intent_f1": {k: round(v, 4) for k, v in r.per_intent_f1.items()},
        "ok": r.ok, "failures": r.failures, "thresholds_calibrated": r.thresholds_calibrated,
    }
