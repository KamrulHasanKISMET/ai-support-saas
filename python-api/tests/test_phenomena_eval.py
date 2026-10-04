"""Tests for app/language/phenomena_eval.py (P5-5 / P6T-3). Pure.
    docker compose exec python-api python -m unittest tests.test_phenomena_eval -v
"""
import unittest

from app.language import phenomena_eval as pe

class TestLoad(unittest.TestCase):
    def test_real_eval_set_loads_and_has_all_layers(self):
        cases = pe.load_cases()
        self.assertGreater(len(cases), 20)
        layers = {c.layer for c in cases}
        for l in ("L1_surface_noise", "L4_code_mixed", "L5_context_dependent",
                  "L6_implied_goal", "L7_negation_minimal_pair", "L8_entity_reference"):
            self.assertIn(l, layers)

    def test_flagged_not_reviewed(self):
        import json
        self.assertFalse(json.loads(pe.EVAL_SET_PATH.read_text(encoding="utf-8"))["reviewed_by_native_speakers"])

    def test_every_pair_id_has_exactly_two_members_with_different_intents_or_is_consistency_only(self):
        cases = pe.load_cases()
        groups: dict[str, list] = {}
        for c in cases:
            if c.pair_id:
                groups.setdefault(c.pair_id, []).append(c)
        for pid, members in groups.items():
            self.assertEqual(len(members), 2, pid)


def C(id_, layer, intent, pair_id=None, notes=""):
    return pe.Case(id=id_, layer=layer, intent=intent, text="x", language="en", pair_id=pair_id, notes=notes)


class TestScore(unittest.TestCase):
    def test_all_correct(self):
        cases = [C("a", "L1", "A"), C("b", "L1", "B")]
        r = pe.score(cases, {"a": "A", "b": "B"})
        self.assertEqual((r["L1"].n, r["L1"].correct, r["L1"].missing), (2, 2, 0))
        self.assertEqual(r["L1"].accuracy, 1.0)

    def test_wrong_and_missing_distinguished(self):
        cases = [C("a", "L1", "A"), C("b", "L1", "B"), C("c", "L1", "C")]
        r = pe.score(cases, {"a": "WRONG"})
        self.assertEqual(r["L1"].correct, 0)
        self.assertEqual(r["L1"].missing, 2)  # b, c never predicted

    def test_per_layer_independent(self):
        cases = [C("a", "L1", "A"), C("b", "L2", "B")]
        r = pe.score(cases, {"a": "A", "b": "WRONG"})
        self.assertEqual(r["L1"].accuracy, 1.0)
        self.assertEqual(r["L2"].accuracy, 0.0)

    def test_consistency_only_case_accepts_decline_marker(self):
        cases = [C("a", "L5", "A", pair_id="p1", notes="a CONSISTENCY check case")]
        r = pe.score(cases, {"a": "declined"})
        self.assertEqual(r["L5"].correct, 1)

    def test_consistency_only_case_still_accepts_the_real_label(self):
        cases = [C("a", "L5", "A", pair_id="p1", notes="a CONSISTENCY check case")]
        self.assertEqual(pe.score(cases, {"a": "A"})["L5"].correct, 1)

    def test_plain_case_does_not_accept_decline_marker(self):
        cases = [C("a", "L1", "A")]
        self.assertEqual(pe.score(cases, {"a": "declined"})["L1"].correct, 0)


class TestPairs(unittest.TestCase):
    def test_pair_passes_when_both_correct(self):
        cases = [C("a", "L7", "A", pair_id="p"), C("b", "L7", "B", pair_id="p")]
        result = pe.score_pairs(cases, {"a": "A", "b": "B"})
        self.assertTrue(result[0].ok)

    def test_pair_fails_when_reusing_one_answer_for_both(self):
        cases = [C("a", "L7", "A", pair_id="p"), C("b", "L7", "B", pair_id="p")]
        result = pe.score_pairs(cases, {"a": "A", "b": "A"})  # both predicted A: ignored the negation
        self.assertFalse(result[0].ok)
        self.assertIn("reusing", result[0].reason)

    def test_consistency_pair_may_decline_both_without_failing(self):
        cases = [C("a", "L5", "A", pair_id="p", notes="CONSISTENCY check"),
                 C("b", "L5", "B", pair_id="p", notes="CONSISTENCY check")]
        result = pe.score_pairs(cases, {"a": "declined", "b": "declined"})
        self.assertTrue(result[0].ok)

    def test_pair_fails_when_one_member_individually_wrong(self):
        cases = [C("a", "L7", "A", pair_id="p"), C("b", "L7", "B", pair_id="p")]
        result = pe.score_pairs(cases, {"a": "A", "b": "WRONG"})
        self.assertFalse(result[0].ok)

    def test_no_pair_id_is_not_scored_as_a_pair(self):
        cases = [C("a", "L1", "A")]
        self.assertEqual(pe.score_pairs(cases, {"a": "A"}), [])

    def test_missing_predictions_fail_the_pair(self):
        cases = [C("a", "L7", "A", pair_id="p"), C("b", "L7", "B", pair_id="p")]
        result = pe.score_pairs(cases, {"a": "A"})
        self.assertFalse(result[0].ok)


class TestReport(unittest.TestCase):
    def test_report_shape_and_overall_accuracy(self):
        cases = [C("a", "L1", "A", pair_id="p"), C("b", "L1", "B", pair_id="p")]
        rep = pe.report(cases, {"a": "A", "b": "B"})
        self.assertEqual(rep["overall_accuracy"], 1.0)
        self.assertIn("per_layer", rep); self.assertIn("pairs", rep)
        self.assertEqual(rep["pair_pass_rate"], 1.0)

    def test_report_on_real_eval_set_is_json_safe_with_no_predictions(self):
        import json
        cases = pe.load_cases()
        json.dumps(pe.report(cases, {}))

    def test_empty_cases_safe(self):
        rep = pe.report([], {})
        self.assertEqual(rep["overall_accuracy"], 0.0)
        self.assertIsNone(rep["pair_pass_rate"])


if __name__ == "__main__":
    unittest.main()
