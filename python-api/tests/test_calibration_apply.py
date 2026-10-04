"""
Tests for app/language/calibration_apply.py. The "evidence" below is
FABRICATED test data to exercise the rules; nothing here is a real
calibration and no real file is touched.

Run:
    docker compose exec python-api python -m unittest tests.test_calibration_apply -v
"""

import unittest

from app.language import calibration_apply as c
from app.language import generalization_eval as g


def _ev(overall=0.90, per_lang=0.85, cov=6, concept=0.85, ok=True, n=130, obs=0.95, weak=0.90):
    if not ok:
        return {"suggestion": {"ok": False, "reason": "only 5 scored cases"}}
    return {"suggestion": {"ok": True, "scored_cases": n,
            "observed": {"overall": obs, "weakest_language": weak},
            "suggested": {"min_overall": overall, "min_per_language": per_lang,
                          "min_cases_per_language": cov, "min_concept_consistency": concept}}}


class TestMerge(unittest.TestCase):
    def test_median_of_runs(self):
        p = c.merge([_ev(overall=0.88), _ev(overall=0.90), _ev(overall=0.92)],
                    set_reviewed=True, accept_unreviewed=False)
        self.assertEqual(p["thresholds"]["min_overall"], 0.90)
        self.assertEqual(p["runs"], 3)

    def test_needs_two_runs(self):
        with self.assertRaises(c.CalibrationRefused) as cm:
            c.merge([_ev()], set_reviewed=True, accept_unreviewed=False)
        self.assertIn("need >=", str(cm.exception))

    def test_no_evidence_refused(self):
        with self.assertRaises(c.CalibrationRefused):
            c.merge([], set_reviewed=True, accept_unreviewed=False)

    def test_not_ok_run_refused(self):
        with self.assertRaises(c.CalibrationRefused) as cm:
            c.merge([_ev(), _ev(ok=False)], set_reviewed=True, accept_unreviewed=False)
        self.assertIn("not ok", str(cm.exception))

    def test_unreviewed_set_refused_by_default(self):
        with self.assertRaises(c.CalibrationRefused) as cm:
            c.merge([_ev(), _ev()], set_reviewed=False, accept_unreviewed=False)
        self.assertIn("native-speaker", str(cm.exception))

    def test_unreviewed_set_allowed_with_explicit_flag(self):
        p = c.merge([_ev(), _ev()], set_reviewed=False, accept_unreviewed=True)
        self.assertFalse(p["eval_set_reviewed"])

    def test_wide_disagreement_refused(self):
        with self.assertRaises(c.CalibrationRefused) as cm:
            c.merge([_ev(overall=0.60), _ev(overall=0.92)], set_reviewed=True, accept_unreviewed=False)
        self.assertIn("disagree", str(cm.exception))

    def test_all_reasons_reported_together(self):
        with self.assertRaises(c.CalibrationRefused) as cm:
            c.merge([_ev(ok=False)], set_reviewed=False, accept_unreviewed=False)
        self.assertEqual(len(str(cm.exception).splitlines()), 3)


class TestPatch(unittest.TestCase):
    def test_patches_real_source_and_flips_flag(self):
        src = c.EVAL_PY.read_text(encoding="utf-8")
        out = c.patch_constants(src, {"min_overall": 0.9, "min_per_language": 0.8,
                                      "min_cases_per_language": 7, "min_concept_consistency": 0.85})
        self.assertIn("MIN_OVERALL_ACCURACY = 0.9", out)
        self.assertIn("MIN_COVERAGE = 7", out)
        self.assertIn("THRESHOLDS_CALIBRATED = True", out)
        compile(out, "generalization_eval.py", "exec")  # still valid Python

    def test_second_apply_refused(self):
        out = c.patch_constants(c.EVAL_PY.read_text(encoding="utf-8"),
                                {"min_overall": 0.9, "min_per_language": 0.8,
                                 "min_cases_per_language": 7, "min_concept_consistency": 0.85})
        with self.assertRaises(c.CalibrationRefused):
            c.patch_constants(out, {"min_overall": 0.9, "min_per_language": 0.8,
                                    "min_cases_per_language": 7, "min_concept_consistency": 0.85})

    def test_missing_constant_refused(self):
        with self.assertRaises(c.CalibrationRefused):
            c.patch_constants("X = 1\nTHRESHOLDS_CALIBRATED = False\n", {})

    def test_shipped_source_is_still_uncalibrated(self):
        self.assertFalse(g.THRESHOLDS_CALIBRATED)

    def test_log_row_and_placeholder_replacement(self):
        p = c.merge([_ev(), _ev()], set_reviewed=False, accept_unreviewed=True)
        row = c.log_row(p, "Alice", "2026-10-01")
        self.assertIn("NOT native-reviewed", row)
        self.assertIn("| Alice |", row)
        doc = "x\n| Date | Tenant(s) |\n|---|---|\n" + c.LOG_PLACEHOLDER + "\n"
        self.assertIn(row, c.patch_log(doc, row))
        self.assertNotIn("no real run yet", c.patch_log(doc, row))

    def test_log_row_appended_after_existing_rows(self):
        doc = "| Date | Tenant(s) |\n|---|---|\n| old | row |\n\ntext\n"
        out = c.patch_log(doc, "| new | row |")
        self.assertLess(out.index("| old | row |"), out.index("| new | row |"))


if __name__ == "__main__":
    unittest.main()
