"""Tests for app/language/experience_backfill_report.py (P6D-8). Pure
grouping logic (summarize_groups); fetch/run are DB-only (pragma: no cover).
    docker compose exec python-api python -m unittest tests.test_experience_backfill_report -v
"""
import unittest

from app.language.experience_backfill_report import Row, summarize_groups


def R(id_, text, intent="PRICE_INQUIRY", lang="en", entities=None):
    return Row(id_, text, entities or [], lang, intent)


class TestSummarizeGroups(unittest.TestCase):
    def test_exact_repeats_collapse_into_one_group(self):
        rows = [R(str(i), "how much is this?") for i in range(50)]
        s = summarize_groups(rows, tenant_id=1)
        self.assertEqual(s.total_rows, 50)
        self.assertEqual(s.distinct_groups, 1)
        self.assertEqual(s.largest_groups[0][1], 50)

    def test_genuine_variety_stays_separate(self):
        texts = ["how much is this?", "what's the price?", "eta koto?", "dam koto?"]
        rows = [R(str(i), t) for i, t in enumerate(texts)]
        s = summarize_groups(rows, tenant_id=1)
        self.assertEqual(s.distinct_groups, 4)
        self.assertEqual(s.group_size_histogram, {"1": 4})

    def test_negation_pair_never_collapses_even_with_same_intent_field_missing(self):
        # different confirmed intents -> must be different groups, matching
        # the safety property in experience_shape.py's GroupKey tests
        rows = [R("a", "i want the red one", intent="CREATE_ORDER"),
                R("b", "i do not want the red one", intent="PRODUCT_INQUIRY")]
        s = summarize_groups(rows, tenant_id=1)
        self.assertEqual(s.distinct_groups, 2)

    def test_entity_values_collapse_shape(self):
        rows = [R(f"o{i}", f"where is order #{1000+i}?", entities=[f"#{1000+i}"]) for i in range(10)]
        s = summarize_groups(rows, tenant_id=1)
        self.assertEqual(s.distinct_groups, 1)
        self.assertEqual(s.largest_groups[0][1], 10)

    def test_cross_tenant_rows_are_not_mixed_by_this_function(self):
        # summarize_groups is called once per tenant by run_for_tenant;
        # passing a fixed tenant_id means two calls for two tenants never
        # share a group key even on identical text (test documents that
        # contract explicitly since tenant_id is a function argument, not
        # a row field here).
        rows = [R("a", "same text")]
        s1 = summarize_groups(rows, tenant_id=1)
        s2 = summarize_groups(rows, tenant_id=2)
        self.assertNotEqual(s1.largest_groups[0][0], s2.largest_groups[0][0])

    def test_histogram_buckets(self):
        rows = ([R(f"a{i}", "x") for i in range(1)]      # bucket "1"
                + [R(f"b{i}", "y", intent="B") for i in range(3)]   # "2-4"
                + [R(f"c{i}", "z", intent="C") for i in range(10)]) # "5-19"
        s = summarize_groups(rows, tenant_id=1)
        self.assertEqual(s.group_size_histogram, {"1": 1, "2-4": 1, "5-19": 1})

    def test_rows_saved_at_candidate_caps(self):
        rows = [R(str(i), "x") for i in range(30)]
        s = summarize_groups(rows, tenant_id=1, candidate_caps=(10, 50))
        self.assertEqual(s.rows_beyond_cap[10], 20)  # 30 - 10 saved
        self.assertEqual(s.rows_beyond_cap[50], 0)   # cap never reached

    def test_empty_input_safe(self):
        s = summarize_groups([], tenant_id=1)
        self.assertEqual((s.total_rows, s.distinct_groups), (0, 0))
        d = s.to_dict()
        self.assertIsNone(d["dedup_ratio"])

    def test_to_dict_is_json_safe(self):
        import json
        rows = [R(str(i), "x" if i % 2 else "y") for i in range(6)]
        json.dumps(summarize_groups(rows, tenant_id=1).to_dict())

    def test_uses_group_key_not_a_reimplemented_rule(self):
        # structural check: this module must not hand-roll its own dedup
        # key logic separate from experience_shape.group_key
        import pathlib
        src = pathlib.Path(__import__("app.language.experience_backfill_report", fromlist=["x"]).__file__).read_text()
        self.assertIn("from app.language.experience_shape import group_key", src)


if __name__ == "__main__":
    unittest.main()
