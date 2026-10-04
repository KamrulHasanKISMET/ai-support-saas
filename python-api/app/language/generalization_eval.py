"""
GENERALIZATION EVALUATION — docs/GENERAL_LANGUAGE_BRAIN.md §5.5.

The question this answers: "Did the Brain learn the GOAL, or memorize
the example sentences it was built from?" Clusters are built from a
tenant's own traffic; a cluster can look great against the LLM on its
own training examples and still fail on a customer who phrases the same
goal in another language or script. This module measures that on
HELD-OUT phrasings (app/language/eval_sets/*.json) that no cluster was
built from.

What is measured, per case:
    raw phrasing -> normalize() -> embed() -> nearest cluster -> intent
compared with the case's expected intent. `normalize` and `embed` are
injected so the SAME code runs
  - offline in unit tests with fakes (tests/test_generalization_eval.py)
  - for real with the production Language Engine + embedding provider
    (run_for_tenant / the CLI below), which is the only way to get a
    meaningful score.

Reported slices (a single overall number would hide exactly the failure
this exists to catch -- e.g. 95% overall while Thai is at 40%):
    overall, by language, by script, native-vs-transliterated, and
    concept_consistency: for each concept (same goal, many languages),
    the share of concepts where EVERY language landed on the same
    predicted intent (cross-language agreement, right or wrong).

Leakage guard: a case whose text (or normalized text) exactly matches a
cluster example is skipped and counted, never scored -- otherwise the
test would reward memorization, the very thing it detects.

Gate: passes_gate() refuses to pass a language with too few cases
(insufficient coverage is a FAIL, not a silent pass).

Wired into PromotionService.check_and_promote() (see promotion_service.py's
_run_generalization_gate): once a canary clears the accuracy bar, the
candidate must also clear this gate -- scoped to that intent's cases
via the `intent` filter below -- before it is promoted. An intent with
NO held-out cases yet is treated as "not applicable" (skip, don't
fail) by PromotionService, since eval coverage is added intent-by-intent
and an intent without cases must not be permanently blocked from
promotion for that reason. run_for_tenant() itself stays usable
standalone (the CLI below) for tenant-wide reports covering every
intent in the eval set at once.

Run for real (needs DB with clusters + embedding provider + LLM key):
    docker compose exec python-api python -m app.language.generalization_eval --tenant 1
    docker compose exec python-api python -m app.language.generalization_eval --tenant 1 --intent PRICE_INQUIRY

HARD BOUNDARY (§5.3): language/script are used here only as *evaluation
slices*, never to infer anything about a person.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

DEFAULT_EVAL_SET = Path(__file__).parent / "eval_sets" / "multilingual_intents_v1.json"


@dataclass(frozen=True)
class EvalCase:
    id: str
    concept_id: str
    intent: str
    text: str
    language: str
    script: str
    transliterated: bool


@dataclass(frozen=True)
class ClusterPoint:
    intent: str
    embedding: list[float]
    example_message: str = ""


@dataclass
class CaseResult:
    case: EvalCase
    normalized: str
    predicted: str | None
    similarity: float | None

    @property
    def correct(self) -> bool:
        return self.predicted == self.case.intent


@dataclass
class Slice:
    n: int = 0
    correct: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.n if self.n else 0.0


@dataclass
class GeneralizationReport:
    results: list[CaseResult] = field(default_factory=list)
    skipped_leaked: list[str] = field(default_factory=list)
    skipped_error: list[str] = field(default_factory=list)
    overall: Slice = field(default_factory=Slice)
    by_language: dict[str, Slice] = field(default_factory=dict)
    by_script: dict[str, Slice] = field(default_factory=dict)
    native: Slice = field(default_factory=Slice)
    transliterated: Slice = field(default_factory=Slice)
    concept_consistency: float = 0.0
    concepts_scored: int = 0

    def misses(self) -> list[CaseResult]:
        return [r for r in self.results if not r.correct]


# ── pure helpers ────────────────────────────────────────────────────


def load_eval_set(path: Path | str = DEFAULT_EVAL_SET) -> list[EvalCase]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        EvalCase(
            id=c["id"], concept_id=c["concept_id"], intent=c["intent"], text=c["text"],
            language=c["language"], script=c["script"], transliterated=bool(c["transliterated"]),
        )
        for c in doc["cases"]
    ]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def nearest_intent(
    query: list[float], points: list[ClusterPoint]
) -> tuple[str | None, float | None]:
    """Same decision the shadow brain makes: the single nearest cluster
    point wins."""
    best_intent, best_sim = None, None
    for p in points:
        sim = cosine(query, p.embedding)
        if best_sim is None or sim > best_sim:
            best_intent, best_sim = p.intent, sim
    return best_intent, best_sim


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _bump(sl: Slice, correct: bool) -> None:
    sl.n += 1
    sl.correct += 1 if correct else 0


# ── evaluation ──────────────────────────────────────────────────────

Normalize = Callable[[str], Awaitable[str]]
Embed = Callable[[str], Awaitable[list[float]]]


async def evaluate(
    cases: list[EvalCase],
    points: list[ClusterPoint],
    normalize: Normalize,
    embed: Embed,
) -> GeneralizationReport:
    report = GeneralizationReport()
    seen_examples = {_norm(p.example_message) for p in points if p.example_message}

    for case in cases:
        if _norm(case.text) in seen_examples:
            report.skipped_leaked.append(case.id)
            continue
        try:
            normalized = await normalize(case.text)
            if _norm(normalized) in seen_examples:
                report.skipped_leaked.append(case.id)
                continue
            emb = await embed(normalized)
            predicted, sim = nearest_intent(emb, points)
        except Exception:
            # One failing case must not abort the run or hide the rest;
            # it is reported, not scored as right or wrong.
            report.skipped_error.append(case.id)
            continue

        res = CaseResult(case, normalized, predicted, sim)
        report.results.append(res)
        _bump(report.overall, res.correct)
        _bump(report.by_language.setdefault(case.language, Slice()), res.correct)
        _bump(report.by_script.setdefault(case.script, Slice()), res.correct)
        _bump(report.transliterated if case.transliterated else report.native, res.correct)

    by_concept: dict[str, set[str | None]] = defaultdict(set)
    for r in report.results:
        by_concept[r.case.concept_id].add(r.predicted)
    if by_concept:
        report.concepts_scored = len(by_concept)
        report.concept_consistency = sum(1 for v in by_concept.values() if len(v) == 1) / len(by_concept)
    return report


# ── gate thresholds ─────────────────────────────────────────────────
#
# STATUS: UNCALIBRATED PLACEHOLDERS. These are starting guesses, not
# measured values. Do NOT edit them by hand from intuition -- run the
# CLI against a real tenant (`--suggest`, see suggest_thresholds()) and
# record the evidence in docs/LANGUAGE_INTELLIGENCE.md's calibration log.
# Names match the ones the docs use (MIN_LANG_ACCURACY / MIN_COVERAGE).
MIN_OVERALL_ACCURACY = 0.85
MIN_LANG_ACCURACY = 0.75          # per-language accuracy floor
MIN_COVERAGE = 5                  # scored cases required per language
MIN_CONCEPT_CONSISTENCY = 0.80
THRESHOLDS_CALIBRATED = False     # flip to True only with logged evidence


@dataclass(frozen=True)
class GateThresholds:
    min_overall: float = MIN_OVERALL_ACCURACY
    min_per_language: float = MIN_LANG_ACCURACY
    min_cases_per_language: int = MIN_COVERAGE
    min_concept_consistency: float = MIN_CONCEPT_CONSISTENCY


def passes_gate(
    report: GeneralizationReport,
    *,
    min_overall: float = MIN_OVERALL_ACCURACY,
    min_per_language: float = MIN_LANG_ACCURACY,
    min_cases_per_language: int = MIN_COVERAGE,
    min_concept_consistency: float = MIN_CONCEPT_CONSISTENCY,
) -> tuple[bool, list[str]]:
    """Returns (passed, reasons_for_failure). Defaults are the module
    constants above, which are still uncalibrated placeholders."""
    reasons: list[str] = []
    if report.overall.n == 0:
        return False, ["no cases were scored"]
    if report.overall.accuracy < min_overall:
        reasons.append(f"overall accuracy {report.overall.accuracy:.2f} < {min_overall}")
    for lang, sl in sorted(report.by_language.items()):
        if sl.n < min_cases_per_language:
            reasons.append(f"{lang}: only {sl.n} scored cases (< {min_cases_per_language}) -- insufficient coverage")
        elif sl.accuracy < min_per_language:
            reasons.append(f"{lang}: accuracy {sl.accuracy:.2f} < {min_per_language}")
    if report.concept_consistency < min_concept_consistency:
        reasons.append(f"cross-language concept consistency {report.concept_consistency:.2f} < {min_concept_consistency}")
    return (not reasons), reasons


# ── calibration support ─────────────────────────────────────────────
#
# We cannot pick thresholds without measurements. suggest_thresholds()
# turns a REAL report into evidence-based *candidates* and says how
# trustworthy they are. It never edits the constants; a human reviews
# the output and updates them together with the calibration log.

MIN_SCORED_FOR_SUGGESTION = 100   # below this a suggestion is refused
SUGGESTION_MARGIN = 0.05          # headroom under the observed value
MIN_CASES_FLOOR = 5               # never suggest coverage below this


def suggest_thresholds(report: GeneralizationReport) -> dict:
    """Derive candidate thresholds from one real report.

    Rule (deliberately simple and conservative): a threshold should sit
    SUGGESTION_MARGIN below what the current Brain actually achieves, so
    the gate blocks regressions but does not fail a healthy Brain on
    noise. Weakest-language accuracy sets the per-language floor.
    Coverage is set to the smallest per-language scored count, but never
    below MIN_CASES_FLOOR. Returns {"ok": False, "reason": ...} when the
    sample is too small or empty -- refusing beats inventing numbers.
    """
    n = report.overall.n
    if n == 0:
        return {"ok": False, "reason": "no cases were scored"}
    if n < MIN_SCORED_FOR_SUGGESTION:
        return {
            "ok": False,
            "reason": f"only {n} scored cases (< {MIN_SCORED_FOR_SUGGESTION}); run against a tenant with real clusters",
        }
    langs = report.by_language
    weakest = min(langs.values(), key=lambda sl: sl.accuracy).accuracy if langs else 0.0
    thin = min((sl.n for sl in langs.values()), default=0)

    def down(x: float) -> float:
        return round(max(0.0, x - SUGGESTION_MARGIN), 2)

    suggested = GateThresholds(
        min_overall=down(report.overall.accuracy),
        min_per_language=down(weakest),
        min_cases_per_language=max(MIN_CASES_FLOOR, min(thin, MIN_CASES_FLOOR * 2)),
        min_concept_consistency=down(report.concept_consistency),
    )
    warnings: list[str] = []
    if report.skipped_error:
        warnings.append(f"{len(report.skipped_error)} cases errored and were not scored")
    if report.skipped_leaked:
        warnings.append(f"{len(report.skipped_leaked)} cases skipped as leaked into clusters")
    if suggested.min_overall < MIN_OVERALL_ACCURACY:
        warnings.append("suggested overall floor is BELOW the placeholder -- the Brain is weaker than the placeholder assumed; fix the Brain rather than lowering the bar")
    return {
        "ok": True,
        "scored_cases": n,
        "observed": {
            "overall": round(report.overall.accuracy, 4),
            "weakest_language": round(weakest, 4),
            "concept_consistency": round(report.concept_consistency, 4),
            "min_language_cases": thin,
        },
        "suggested": asdict(suggested),
        "warnings": warnings,
        "note": "Candidates only. Review, then update the constants and the calibration log together. Dataset is AI-drafted and not native-speaker reviewed.",
    }


def report_to_dict(report: GeneralizationReport) -> dict:
    """Machine-readable snapshot (for --json / saving calibration runs)."""

    def sl(x: Slice) -> dict:
        return {"n": x.n, "correct": x.correct, "accuracy": round(x.accuracy, 4)}

    return {
        "overall": sl(report.overall),
        "by_language": {k: sl(v) for k, v in sorted(report.by_language.items())},
        "by_script": {k: sl(v) for k, v in sorted(report.by_script.items())},
        "native": sl(report.native),
        "transliterated": sl(report.transliterated),
        "concept_consistency": round(report.concept_consistency, 4),
        "concepts_scored": report.concepts_scored,
        "skipped_leaked": len(report.skipped_leaked),
        "skipped_error": len(report.skipped_error),
        "misses": [
            {"id": m.case.id, "expected": m.case.intent, "predicted": m.predicted, "similarity": m.similarity}
            for m in report.misses()
        ],
    }


def format_report(report: GeneralizationReport) -> str:
    def line(name: str, sl: Slice) -> str:
        return f"  {name:<14} {sl.correct:>3}/{sl.n:<3} {sl.accuracy:6.1%}"

    out = ["GENERALIZATION REPORT (held-out multilingual phrasings)", line("overall", report.overall)]
    out.append("by language:")
    out += [line(k, v) for k, v in sorted(report.by_language.items())]
    out.append("by script:")
    out += [line(k, v) for k, v in sorted(report.by_script.items())]
    out += ["native vs transliterated:", line("native", report.native), line("transliterated", report.transliterated)]
    out.append(f"cross-language concept consistency: {report.concept_consistency:.1%} of {report.concepts_scored} concepts")
    out.append(f"skipped (leaked into clusters): {len(report.skipped_leaked)}; skipped (errors): {len(report.skipped_error)}")
    misses = report.misses()
    if misses:
        out.append(f"misses ({len(misses)}):")
        for m in misses:
            out.append(f"  {m.case.id}: expected {m.case.intent}, got {m.predicted} (sim={m.similarity:.2f}) via '{m.normalized}'")
    return "\n".join(out)


# ── real run (production wiring) ────────────────────────────────────


async def load_cluster_points(db, tenant_id: int) -> list[ClusterPoint]:
    """Same population the shadow brain matches against."""
    from sqlalchemy import text

    rows = (
        await db.execute(
            text(
                "SELECT intent, embedding::text AS emb, example_message "
                "FROM intent_clusters WHERE tenant_id = :t"
            ),
            {"t": tenant_id},
        )
    ).all()
    return [ClusterPoint(r.intent, json.loads(r.emb), r.example_message or "") for r in rows]


async def run_for_tenant(
    db,
    tenant_id: int,
    eval_path: Path | str = DEFAULT_EVAL_SET,
    *,
    intent: str | None = None,
) -> GeneralizationReport:
    """
    intent: when given, scores only the eval cases whose `intent`
    matches (used by PromotionService to gate one intent's promotion
    without paying for or reporting on the whole tenant-wide set).
    `points` (the nearest-neighbor population) is always every cluster
    for the tenant, regardless of `intent` -- that population is what
    the real matcher (ShadowBrain) matches against, so scoring a
    narrower population here would not reflect production behavior;
    only which CASES get scored is narrowed.
    """
    from app.ai.embedding_service import embedding_service
    from app.language.language_engine import language_engine

    async def normalize(text_: str) -> str:
        return (await language_engine.understand(text_)).normalized_message

    points = await load_cluster_points(db, tenant_id)
    cases = load_eval_set(eval_path)
    if intent is not None:
        cases = [c for c in cases if c.intent == intent]
    return await evaluate(cases, points, normalize, embedding_service.embed)


async def _main(tenant_id: int, eval_path: str, intent: str | None, as_json: bool = False, suggest: bool = False) -> int:
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        report = await run_for_tenant(db, tenant_id, eval_path, intent=intent)
    if suggest:
        print(json.dumps({"report": report_to_dict(report), "suggestion": suggest_thresholds(report)}, indent=2, ensure_ascii=False))
        return 0
    if as_json:
        print(json.dumps(report_to_dict(report), indent=2, ensure_ascii=False))
        return 0
    print(format_report(report))
    if not THRESHOLDS_CALIBRATED:
        print("\nNOTE: gate thresholds are still uncalibrated placeholders (see THRESHOLDS_CALIBRATED).")
    ok, reasons = passes_gate(report)
    print("\nGATE:", "PASS" if ok else "FAIL")
    for r in reasons:
        print(" -", r)
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--eval-set", default=str(DEFAULT_EVAL_SET))
    ap.add_argument("--intent", default=None, help="Score only this intent's held-out cases (matches PromotionService's per-intent gate).")
    ap.add_argument("--json", action="store_true", help="Print the machine-readable report and exit 0 (save it as calibration evidence).")
    ap.add_argument("--suggest", action="store_true", help="Print the report plus candidate gate thresholds derived from it (never edits any file).")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_main(args.tenant, args.eval_set, args.intent, args.json, args.suggest)))
