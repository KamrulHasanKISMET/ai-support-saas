"""
CALIBRATION APPLY (docs/PENDING_WORK.md A1, runbook step 4).

Turns saved `generalization_eval --suggest` outputs (REAL runs) into the
constants + calibration-log row -- mechanically, and only when the
evidence is good enough. It cannot invent numbers: with no evidence
files it does nothing. Default is a dry run that prints the proposed
change; --write edits generalization_eval.py and the docs log.

Refuses when:
  * fewer than MIN_EVIDENCE_FILES runs were given (one run = one moment,
    one tenant; the runbook asks for 2-3),
  * any run has suggestion.ok == false (too few scored cases),
  * the eval set is not native-speaker reviewed, unless you pass
    --accept-unreviewed-set (then the log row says so).

Merge rule: the most CONSERVATIVE suggestion across runs is NOT used --
that would let the weakest tenant set everyone's bar. It takes the
MEDIAN of each threshold across runs, and prints the min/max spread so
a human can see disagreement; a spread > MAX_SPREAD refuses (the runs
disagree too much to trust one number).

    python -m app.language.generalization_eval --tenant 1 --suggest > run1.json
    python -m app.language.generalization_eval --tenant 2 --suggest > run2.json
    python -m app.language.calibration_apply run1.json run2.json --reviewer "Name"
    python -m app.language.calibration_apply run1.json run2.json --reviewer "Name" --write
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from datetime import date
from pathlib import Path

MIN_EVIDENCE_FILES = 2
MAX_SPREAD = 0.15   # max (max-min) of any accuracy-type threshold across runs

_FIELDS = ("min_overall", "min_per_language", "min_cases_per_language", "min_concept_consistency")
_CONST_FOR = {
    "min_overall": "MIN_OVERALL_ACCURACY",
    "min_per_language": "MIN_LANG_ACCURACY",
    "min_cases_per_language": "MIN_COVERAGE",
    "min_concept_consistency": "MIN_CONCEPT_CONSISTENCY",
}
ROOT = Path(__file__).resolve().parents[2]
EVAL_PY = Path(__file__).with_name("generalization_eval.py")
EVAL_SET = Path(__file__).with_name("eval_sets") / "multilingual_intents_v1.json"
LOG_DOC = ROOT.parent / "docs" / "LANGUAGE_INTELLIGENCE.md"
LOG_PLACEHOLDER = "| — | — | — | — | *(no real run yet — placeholders in force)* | — |"


class CalibrationRefused(ValueError):
    pass


def merge(evidence: list[dict], *, set_reviewed: bool, accept_unreviewed: bool) -> dict:
    """Pure. evidence = parsed --suggest outputs. Returns the proposal or
    raises CalibrationRefused with every reason."""
    reasons: list[str] = []
    if len(evidence) < MIN_EVIDENCE_FILES:
        reasons.append(f"need >= {MIN_EVIDENCE_FILES} evidence runs, got {len(evidence)}")
    sugg = []
    for i, e in enumerate(evidence, 1):
        s = e.get("suggestion") or {}
        if not s.get("ok"):
            reasons.append(f"run {i}: suggestion not ok ({s.get('reason', 'missing')})")
        else:
            sugg.append(s)
    if not set_reviewed and not accept_unreviewed:
        reasons.append("eval set is not native-speaker reviewed (PENDING_WORK A3); "
                       "pass --accept-unreviewed-set to proceed anyway")
    if reasons:
        raise CalibrationRefused("\n".join(reasons))

    merged, spread = {}, {}
    for f in _FIELDS:
        vals = [s["suggested"][f] for s in sugg]
        med = statistics.median(vals)
        merged[f] = int(round(med)) if f == "min_cases_per_language" else round(med, 2)
        spread[f] = round(max(vals) - min(vals), 4)
    too_wide = [f for f in _FIELDS if f != "min_cases_per_language" and spread[f] > MAX_SPREAD]
    if too_wide:
        raise CalibrationRefused(
            f"runs disagree too much (spread > {MAX_SPREAD}) on {too_wide}: {spread}. "
            "Investigate the runs (bad cluster? bad eval cases?) before trusting a number.")
    return {
        "thresholds": merged,
        "spread": spread,
        "runs": len(sugg),
        "scored_cases": sum(s["scored_cases"] for s in sugg),
        "observed_overall": [s["observed"]["overall"] for s in sugg],
        "observed_weakest_language": [s["observed"]["weakest_language"] for s in sugg],
        "eval_set_reviewed": set_reviewed,
    }


def patch_constants(source: str, thresholds: dict) -> str:
    """Rewrite the four constants and flip THRESHOLDS_CALIBRATED. Raises
    if a constant line is not found exactly once (never a silent no-op)."""
    for field, const in _CONST_FOR.items():
        pat = re.compile(rf"^({const}\s*=\s*)[0-9.]+", re.M)
        if len(pat.findall(source)) != 1:
            raise CalibrationRefused(f"constant {const} not found exactly once in generalization_eval.py")
        source = pat.sub(lambda m: f"{m.group(1)}{thresholds[field]}", source, count=1)
    flag = re.compile(r"^(THRESHOLDS_CALIBRATED\s*=\s*)False", re.M)
    if len(flag.findall(source)) != 1:
        raise CalibrationRefused("THRESHOLDS_CALIBRATED = False not found exactly once (already calibrated?)")
    return flag.sub(lambda m: f"{m.group(1)}True", source, count=1)


def log_row(proposal: dict, reviewer: str, today: str) -> str:
    t = proposal["thresholds"]
    obs = f"{min(proposal['observed_overall']):.2f}–{max(proposal['observed_overall']):.2f} / weakest {min(proposal['observed_weakest_language']):.2f}"
    consts = (f"overall {t['min_overall']}, per-lang {t['min_per_language']}, "
              f"coverage {t['min_cases_per_language']}, concept {t['min_concept_consistency']}")
    note = "" if proposal["eval_set_reviewed"] else " (eval set NOT native-reviewed)"
    return f"| {today} | {proposal['runs']} runs | {proposal['scored_cases']} | {obs} | {consts}{note} | {reviewer} |"


def patch_log(doc: str, row: str) -> str:
    if LOG_PLACEHOLDER in doc:
        return doc.replace(LOG_PLACEHOLDER, row, 1)
    marker = "| Date | Tenant(s) |"
    if marker not in doc:
        raise CalibrationRefused("calibration log table not found in LANGUAGE_INTELLIGENCE.md")
    lines = doc.split("\n")
    last = max(i for i, l in enumerate(lines) if l.startswith("|") and i > doc[:doc.index(marker)].count("\n"))
    lines.insert(last + 1, row)
    return "\n".join(lines)


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("evidence", nargs="*", help="saved --suggest JSON outputs from REAL runs")
    ap.add_argument("--reviewer", default="", help="who reviewed and approved these numbers (required with --write)")
    ap.add_argument("--accept-unreviewed-set", action="store_true")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    try:
        evidence = [json.loads(Path(p).read_text(encoding="utf-8")) for p in a.evidence]
        reviewed = bool(json.loads(EVAL_SET.read_text(encoding="utf-8")).get("reviewed_by_native_speakers"))
        proposal = merge(evidence, set_reviewed=reviewed, accept_unreviewed=a.accept_unreviewed_set)
    except CalibrationRefused as e:
        print("REFUSED:\n" + str(e))
        return 1
    print(json.dumps(proposal, indent=2))
    if not a.write:
        print("\n(dry run -- nothing changed; add --write --reviewer NAME to apply)")
        return 0
    if not a.reviewer.strip():
        print("REFUSED: --reviewer is required with --write")
        return 1
    try:
        new_src = patch_constants(EVAL_PY.read_text(encoding="utf-8"), proposal["thresholds"])
        new_doc = patch_log(LOG_DOC.read_text(encoding="utf-8"),
                            log_row(proposal, a.reviewer.strip(), date.today().isoformat()))
    except (CalibrationRefused, FileNotFoundError) as e:
        print("REFUSED:", e)
        return 1
    EVAL_PY.write_text(new_src, encoding="utf-8")
    LOG_DOC.write_text(new_doc, encoding="utf-8")
    print("written: constants + THRESHOLDS_CALIBRATED=True + log row")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
