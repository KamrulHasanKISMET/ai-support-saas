"""
Tests for app/language/language_tag_readiness.py (pure decision logic).
Says nothing about real traffic -- it pins the rule: insufficient data
is NOT READY, any 'und' row is NOT READY, clean and large enough is READY.

Run:
    docker compose exec python-api python -m unittest tests.test_language_tag_readiness -v
"""

import unittest

from app.language.language_tag_readiness import MIN_ROWS_FOR_DECISION, assess


class TestAssess(unittest.TestCase):
    def test_no_rows_is_not_ready(self):
        r = assess(0, 0)
        self.assertFalse(r.ready)
        self.assertEqual(r.und_rate, 0.0)

    def test_too_little_traffic_is_not_ready_even_if_clean(self):
        r = assess(MIN_ROWS_FOR_DECISION - 1, 0)
        self.assertFalse(r.ready)
        self.assertIn("not enough real traffic", r.reasons[0])

    def test_exactly_min_rows_and_clean_is_ready(self):
        r = assess(MIN_ROWS_FOR_DECISION, 0)
        self.assertTrue(r.ready)
        self.assertEqual(r.reasons, [])

    def test_single_und_row_blocks_removal(self):
        r = assess(MIN_ROWS_FOR_DECISION * 10, 1)
        self.assertFalse(r.ready)
        self.assertIn("language='und'", r.reasons[0])

    def test_custom_tolerance_allows_small_und_rate(self):
        r = assess(1000, 5, max_und_rate=0.01)
        self.assertTrue(r.ready)

    def test_und_rows_clamped_to_total(self):
        r = assess(600, 9999)
        self.assertEqual(r.und_rows, 600)
        self.assertFalse(r.ready)

    def test_both_reasons_reported(self):
        r = assess(10, 3)
        self.assertEqual(len(r.reasons), 2)


if __name__ == "__main__":
    unittest.main()
