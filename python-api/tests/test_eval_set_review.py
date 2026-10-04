"""
Tests for app/language/eval_set_review.py. Uses SYNTHETIC cases and
synthetic "reviews" -- these pin the tooling's rules (esp. that the
native-speaker flag cannot flip early); they are NOT reviews of the
real dataset.

Run:
    docker compose exec python-api python -m unittest tests.test_eval_set_review -v
"""

import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

from app.language import eval_set_review as r


def _c(id, concept, lang, script, text, translit=False, intent="PRICE_INQUIRY"):
    return dict(id=id, concept_id=concept, intent=intent, text=text,
                language=lang, script=script, transliterated=translit)


def _good():
    return [
        _c("p.en", "p", "en", "latin", "How much is this?"),
        _c("p.bn", "p", "bn", "bengali", "এটার দাম কত?"),
        _c("p.bn-latn", "p", "bn", "latin", "eta koto taka?", True),
    ]


class TestLint(unittest.TestCase):
    def test_clean_set_has_no_problems(self):
        self.assertEqual(r.lint(_good()), [])

    def test_real_set_is_structurally_clean(self):
        doc = json.loads(r.DEFAULT_SET.read_text(encoding="utf-8"))
        self.assertEqual(r.lint(doc["cases"]), [])

    def test_script_mismatch_detected(self):
        cs = _good(); cs[1]["text"] = "how much is it"
        self.assertTrue(any("bengali" in p for p in r.lint(cs)))

    def test_latin_non_native_must_be_transliterated(self):
        cs = _good(); cs[2]["transliterated"] = False
        self.assertTrue(any("must be transliterated" in p for p in r.lint(cs)))

    def test_native_latin_language_not_transliterated(self):
        cs = _good(); cs[0]["transliterated"] = True
        self.assertTrue(any("natively Latin" in p for p in r.lint(cs)))

    def test_transliterated_with_non_latin_script(self):
        cs = _good(); cs[1]["transliterated"] = True
        self.assertTrue(any("transliterated=true but script" in p for p in r.lint(cs)))

    def test_duplicate_id_and_text(self):
        cs = _good() + [dict(_good()[0])]
        probs = r.lint(cs)
        self.assertTrue(any("duplicate id" in p for p in probs))
        self.assertTrue(any("same text" in p for p in probs))

    def test_missing_concept_variant(self):
        # Variants are inferred from the data, so the gap only shows when
        # another concept still has that variant.
        cs = _good() + [
            _c("q.en", "q", "en", "latin", "Where is my order?", intent="ORDER_STATUS"),
            _c("q.bn", "q", "bn", "bengali", "আমার অর্ডার কোথায়?", intent="ORDER_STATUS"),
        ]
        probs = r.lint(cs)
        self.assertTrue(any("concept q" in p and "missing variants" in p for p in probs))

    def test_concept_with_two_intents(self):
        cs = _good(); cs[1]["intent"] = "COMPLAINT"
        self.assertTrue(any("more than one intent" in p for p in r.lint(cs)))

    def test_empty_text(self):
        cs = _good(); cs[0]["text"] = "  "
        self.assertTrue(any("empty text" in p for p in r.lint(cs)))


class TestExportAndApply(unittest.TestCase):
    def setUp(self):
        self.doc = {"reviewed_by_native_speakers": False, "cases": _good()}
        self.known = {c["id"]: c for c in self.doc["cases"]}

    def _sheet(self, rows):
        d = Path(tempfile.mkdtemp()); p = d / "s.csv"
        with p.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=r.SHEET_FIELDS); w.writeheader()
            for row in rows:
                w.writerow({k: row.get(k, "") for k in r.SHEET_FIELDS})
        return p

    def test_export_one_sheet_per_variant_with_english_reference(self):
        d = Path(tempfile.mkdtemp())
        paths = r.export_sheets(self.doc, d)
        self.assertEqual(sorted(p.stem for p in paths), ["bn-bengali", "bn-latin", "en-latin"])
        rows = list(csv.DictReader((d / "bn-bengali.csv").open(encoding="utf-8-sig")))
        self.assertEqual(rows[0]["english_reference"], "How much is this?")

    def test_sheet_without_reviewer_refused(self):
        p = self._sheet([{"id": "p.bn", "verdict": "ok", "reviewer": ""}])
        with self.assertRaises(r.ReviewError):
            r.parse_sheet(p, self.known)

    def test_bad_verdict_and_unknown_id_refused(self):
        p = self._sheet([{"id": "nope", "verdict": "ok", "reviewer": "A"},
                         {"id": "p.bn", "verdict": "maybe", "reviewer": "A"}])
        with self.assertRaises(r.ReviewError) as cm:
            r.parse_sheet(p, self.known)
        self.assertIn("unknown id", str(cm.exception))
        self.assertIn("verdict must be", str(cm.exception))

    def test_fix_requires_corrected_text(self):
        p = self._sheet([{"id": "p.bn", "verdict": "fix", "reviewer": "A"}])
        with self.assertRaises(r.ReviewError):
            r.parse_sheet(p, self.known)

    def test_partial_review_does_not_flip_flag(self):
        rows = [{"id": "p.bn", "verdict": "ok", "reviewer": "Rahim", "corrected_text": "", "notes": ""}]
        doc, side = r.apply_reviews(copy.deepcopy(self.doc), {"reviews": {}}, rows, today="2026-10-01")
        self.assertFalse(doc["reviewed_by_native_speakers"])
        self.assertEqual(side["reviews"]["p.bn"]["reviewer"], "Rahim")

    def test_full_review_flips_flag(self):
        rows = [{"id": i, "verdict": "ok", "reviewer": "X", "corrected_text": "", "notes": ""} for i in self.known]
        doc, _ = r.apply_reviews(copy.deepcopy(self.doc), {"reviews": {}}, rows)
        self.assertTrue(doc["reviewed_by_native_speakers"])

    def test_one_reject_keeps_flag_false(self):
        rows = [{"id": i, "verdict": "ok", "reviewer": "X", "corrected_text": "", "notes": ""} for i in self.known]
        rows[0]["verdict"] = "reject"
        doc, _ = r.apply_reviews(copy.deepcopy(self.doc), {"reviews": {}}, rows)
        self.assertFalse(doc["reviewed_by_native_speakers"])

    def test_fix_applies_corrected_text(self):
        rows = [{"id": "p.bn", "verdict": "fix", "reviewer": "X", "corrected_text": "দাম কত?", "notes": ""}]
        doc, _ = r.apply_reviews(copy.deepcopy(self.doc), {"reviews": {}}, rows)
        self.assertEqual({c["id"]: c for c in doc["cases"]}["p.bn"]["text"], "দাম কত?")

    def test_reviews_accumulate_across_sheets(self):
        a = [{"id": "p.en", "verdict": "ok", "reviewer": "A", "corrected_text": "", "notes": ""}]
        b = [{"id": "p.bn", "verdict": "ok", "reviewer": "B", "corrected_text": "", "notes": ""}]
        d1, s1 = r.apply_reviews(copy.deepcopy(self.doc), {"reviews": {}}, a)
        _, s2 = r.apply_reviews(d1, s1, b)
        self.assertEqual(set(s2["reviews"]), {"p.en", "p.bn"})

    def test_coverage_counts_per_variant(self):
        rev = {"p.bn": {"verdict": "ok", "reviewer": "A"}}
        cov = r.coverage(self.doc, rev)
        self.assertEqual(cov["bn-bengali"], (1, 1))
        self.assertEqual(cov["bn-latin"], (0, 1))

    def test_real_dataset_flag_is_still_false(self):
        doc = json.loads(r.DEFAULT_SET.read_text(encoding="utf-8"))
        self.assertFalse(doc["reviewed_by_native_speakers"])  # only a human may change this


if __name__ == "__main__":
    unittest.main()
