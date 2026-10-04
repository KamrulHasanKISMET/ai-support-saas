"""
EVAL SET REVIEW TOOLING (docs/PENDING_WORK.md A3).

The held-out set (eval_sets/multilingual_intents_v1.json) was drafted by
an AI and has NOT been reviewed by native speakers. This module cannot
review it -- only a human native speaker can. It does the parts that do
not need one, and makes the human part mechanical and auditable:

  lint    automated structural/script checks (catches dataset bugs a
          reviewer should not waste time on). Not a language review.
  export  one CSV per language variant for reviewers (with the English
          reference text of the same concept).
  apply   read completed sheets, validate, apply corrections, record
          WHO reviewed WHAT in a sidecar file. The top-level
          `reviewed_by_native_speakers` flag flips to true ONLY when
          every language variant has a named reviewer, no rejected
          rows, and no unreviewed rows -- never before.

    python -m app.language.eval_set_review lint
    python -m app.language.eval_set_review export --out review_sheets/
    python -m app.language.eval_set_review apply review_sheets/bn-latn.csv [...]
"""

from __future__ import annotations

import argparse
import csv
import json
import unicodedata
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

DEFAULT_SET = Path(__file__).parent / "eval_sets" / "multilingual_intents_v1.json"
SIDECAR_NAME = "multilingual_intents_v1.review.json"

# Unicode-name prefixes that identify each declared script.
_SCRIPT_MARKERS = {
    "arabic": ("ARABIC",), "bengali": ("BENGALI",), "devanagari": ("DEVANAGARI",),
    "hangul": ("HANGUL",), "cyrillic": ("CYRILLIC",), "thai": ("THAI",),
    "han": ("CJK UNIFIED", "CJK COMPATIBILITY", "HIRAGANA", "KATAKANA"),
    "latin": ("LATIN",),
}
# Languages whose native script is Latin: transliterated must be False.
_NATIVE_LATIN = {"en", "es", "tr"}
VERDICTS = ("ok", "fix", "reject")


def _letter_scripts(text: str) -> Counter:
    c: Counter = Counter()
    for ch in text:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        for script, prefixes in _SCRIPT_MARKERS.items():
            if name.startswith(prefixes):
                c[script] += 1
                break
        else:
            c["other"] += 1
    return c


def variant_key(case: dict) -> str:
    """'bn-latin' style key: one reviewer sheet per language+script."""
    return f"{case['language']}-{case['script']}"


def lint(cases: list[dict]) -> list[str]:
    """Structural problems. Empty list = none found (NOT = reviewed)."""
    problems: list[str] = []
    ids = Counter(c["id"] for c in cases)
    problems += [f"duplicate id: {i}" for i, n in ids.items() if n > 1]

    seen_text: dict[tuple[str, str], str] = {}
    for c in cases:
        cid, text, lang, script = c["id"], (c.get("text") or "").strip(), c["language"], c["script"]
        if not text:
            problems.append(f"{cid}: empty text")
            continue
        key = (lang, text.casefold())
        if key in seen_text:
            problems.append(f"{cid}: same text as {seen_text[key]} within language {lang}")
        seen_text[key] = cid

        if script not in _SCRIPT_MARKERS:
            problems.append(f"{cid}: unknown script '{script}'")
            continue
        letters = _letter_scripts(text)
        total = sum(letters.values())
        if total == 0:
            problems.append(f"{cid}: no letters in text")
            continue
        share = letters[script] / total
        if share < 0.9:
            problems.append(f"{cid}: only {share:.0%} of letters are {script} (dominant: {letters.most_common(1)[0][0]})")

        translit = bool(c["transliterated"])
        if translit and script != "latin":
            problems.append(f"{cid}: transliterated=true but script={script}")
        if lang in _NATIVE_LATIN and translit:
            problems.append(f"{cid}: {lang} is natively Latin-script; transliterated should be false")
        if script == "latin" and lang not in _NATIVE_LATIN and not translit:
            problems.append(f"{cid}: Latin-script {lang} must be transliterated=true")
        if not (2 <= len(text) <= 300):
            problems.append(f"{cid}: suspicious length {len(text)}")

    # Every concept must exist in every language variant (a gap silently
    # skews the cross-language consistency metric).
    variants = {variant_key(c) for c in cases}
    by_concept: dict[str, set[str]] = defaultdict(set)
    intent_of: dict[str, set[str]] = defaultdict(set)
    for c in cases:
        by_concept[c["concept_id"]].add(variant_key(c))
        intent_of[c["concept_id"]].add(c["intent"])
    for concept, have in sorted(by_concept.items()):
        missing = variants - have
        if missing:
            problems.append(f"concept {concept}: missing variants {sorted(missing)}")
        if len(intent_of[concept]) > 1:
            problems.append(f"concept {concept}: maps to more than one intent {sorted(intent_of[concept])}")
    return problems


SHEET_FIELDS = ["id", "concept_id", "intent", "english_reference", "text",
                "verdict", "corrected_text", "reviewer", "notes"]


def export_sheets(doc: dict, out_dir: Path) -> list[Path]:
    cases = doc["cases"]
    en_ref = {c["concept_id"]: c["text"] for c in cases if c["language"] == "en"}
    by_variant: dict[str, list[dict]] = defaultdict(list)
    for c in cases:
        by_variant[variant_key(c)].append(c)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for key, rows in sorted(by_variant.items()):
        p = out_dir / f"{key}.csv"
        with p.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=SHEET_FIELDS)
            w.writeheader()
            for c in rows:
                w.writerow({"id": c["id"], "concept_id": c["concept_id"], "intent": c["intent"],
                            "english_reference": en_ref.get(c["concept_id"], ""), "text": c["text"]})
        paths.append(p)
    return paths


class ReviewError(ValueError):
    pass


def parse_sheet(path: Path, known_ids: dict[str, dict]) -> list[dict]:
    """Validate one completed sheet. Raises ReviewError listing every problem."""
    errors: list[str] = []
    rows: list[dict] = []
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        for i, r in enumerate(csv.DictReader(f), start=2):
            cid = (r.get("id") or "").strip()
            verdict = (r.get("verdict") or "").strip().lower()
            reviewer = (r.get("reviewer") or "").strip()
            fixed = (r.get("corrected_text") or "").strip()
            if cid not in known_ids:
                errors.append(f"line {i}: unknown id '{cid}'")
                continue
            if verdict not in VERDICTS:
                errors.append(f"line {i} ({cid}): verdict must be one of {VERDICTS}, got '{verdict}'")
            if not reviewer:
                errors.append(f"line {i} ({cid}): reviewer name is required")
            if verdict == "fix" and not fixed:
                errors.append(f"line {i} ({cid}): verdict=fix needs corrected_text")
            rows.append({"id": cid, "verdict": verdict, "reviewer": reviewer,
                         "corrected_text": fixed, "notes": (r.get("notes") or "").strip()})
    if errors:
        raise ReviewError("\n".join(errors))
    return rows


def apply_reviews(doc: dict, sidecar: dict, rows: list[dict], today: str | None = None) -> tuple[dict, dict]:
    """Pure: returns (new_doc, new_sidecar). Applies 'fix' text; records
    per-case verdict + reviewer. Recomputes the top-level flag."""
    today = today or date.today().isoformat()
    cases = {c["id"]: c for c in doc["cases"]}
    reviews = dict(sidecar.get("reviews", {}))
    for r in rows:
        if r["verdict"] == "fix":
            cases[r["id"]]["text"] = r["corrected_text"]
        reviews[r["id"]] = {"verdict": r["verdict"], "reviewer": r["reviewer"],
                            "date": today, "notes": r["notes"]}
    doc = {**doc, "cases": list(cases.values())}
    doc["reviewed_by_native_speakers"] = fully_reviewed(doc, reviews)
    return doc, {"reviews": reviews}


def fully_reviewed(doc: dict, reviews: dict) -> bool:
    """True only if EVERY case has a review with verdict ok/fix and a
    named reviewer. One reject or one missing row keeps it False."""
    for c in doc["cases"]:
        r = reviews.get(c["id"])
        if not r or r["verdict"] not in ("ok", "fix") or not r["reviewer"]:
            return False
    return True


def coverage(doc: dict, reviews: dict) -> dict[str, tuple[int, int]]:
    out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for c in doc["cases"]:
        k = variant_key(c)
        out[k][1] += 1
        r = reviews.get(c["id"])
        if r and r["verdict"] in ("ok", "fix"):
            out[k][0] += 1
    return {k: (v[0], v[1]) for k, v in sorted(out.items())}


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", default=str(DEFAULT_SET))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("lint")
    ex = sub.add_parser("export"); ex.add_argument("--out", required=True)
    ap_apply = sub.add_parser("apply"); ap_apply.add_argument("sheets", nargs="+")
    sub.add_parser("status")
    a = ap.parse_args()

    set_path = Path(a.set)
    doc = json.loads(set_path.read_text(encoding="utf-8"))
    sidecar_path = set_path.with_name(SIDECAR_NAME)
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8")) if sidecar_path.exists() else {"reviews": {}}

    if a.cmd == "lint":
        probs = lint(doc["cases"])
        print("\n".join(probs) if probs else "lint: no structural problems (this is NOT a native-speaker review)")
        return 1 if probs else 0
    if a.cmd == "export":
        for p in export_sheets(doc, Path(a.out)):
            print("wrote", p)
        return 0
    if a.cmd == "status":
        for k, (done, total) in coverage(doc, sidecar["reviews"]).items():
            print(f"{k:<16} {done}/{total} reviewed")
        print("reviewed_by_native_speakers:", doc.get("reviewed_by_native_speakers"))
        return 0
    known = {c["id"]: c for c in doc["cases"]}
    rows: list[dict] = []
    try:
        for s in a.sheets:
            rows += parse_sheet(Path(s), known)
    except ReviewError as e:
        print("REFUSED -- fix the sheet(s):\n" + str(e))
        return 1
    doc, sidecar = apply_reviews(doc, sidecar, rows)
    set_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"applied {len(rows)} reviews; reviewed_by_native_speakers={doc['reviewed_by_native_speakers']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
