"""Tests for pricing.py and drift_detection.py (Phase 8). Pure.
    docker compose exec python-api python -m unittest tests.test_pricing_and_drift -v
"""
import unittest

from app.language import drift_detection as dr
from app.language import pricing as pr

TABLE = pr.parse_pricing({"m": {"input_per_mtok": 3.0, "output_per_mtok": 15.0, "currency": "USD"}})


class TestPricing(unittest.TestCase):
    def test_no_prices_hardcoded(self):
        self.assertEqual(pr.parse_pricing(None), {})
        self.assertEqual(pr.parse_pricing(""), {})

    def test_cost_math(self):
        self.assertAlmostEqual(pr.cost("m", 1_000_000, 1_000_000, TABLE), 18.0)

    def test_unknown_model_or_usage_is_none_not_zero(self):
        self.assertIsNone(pr.cost("zzz", 10, 10, TABLE))
        self.assertIsNone(pr.cost("m", None, 10, TABLE))
        self.assertEqual(pr.cost("m", 0, 0, TABLE), 0.0)   # reported zero is distinct

    def test_json_string_table(self):
        t = pr.parse_pricing('{"m": {"input_per_mtok": 1, "output_per_mtok": 2}}')
        self.assertEqual(t["m"].currency, "USD")

    def test_malformed_tables_are_loud(self):
        for bad in ('{"m": {"input_per_mtok": -1, "output_per_mtok": 2}}', '{"m": {}}', "not json", '{"m": 5}'):
            with self.assertRaises(pr.PricingError):
                pr.parse_pricing(bad)

    def test_savings_estimate(self):
        s = pr.estimate_savings(model="m", calls_saved=1000, mean_input_tokens=400, mean_output_tokens=60, table=TABLE)
        per_call = (400 * 3 + 60 * 15) / 1e6
        self.assertAlmostEqual(s["saved"], 1000 * per_call)
        self.assertTrue(s["estimate"])

    def test_savings_unknown_when_mean_unmeasured_or_no_price(self):
        a = pr.estimate_savings(model="m", calls_saved=5, mean_input_tokens=None, mean_output_tokens=None, table=TABLE)
        b = pr.estimate_savings(model="x", calls_saved=5, mean_input_tokens=1, mean_output_tokens=1, table=TABLE)
        for r in (a, b):
            self.assertIsNone(r["saved"]); self.assertTrue(r["why_unknown"])

    def test_zero_calls_saved_is_zero_when_priced(self):
        self.assertEqual(pr.estimate_savings(model="m", calls_saved=0, mean_input_tokens=1, mean_output_tokens=1, table=TABLE)["saved"], 0.0)

    def test_negative_calls_rejected(self):
        with self.assertRaises(pr.PricingError):
            pr.estimate_savings(model="m", calls_saved=-1, mean_input_tokens=1, mean_output_tokens=1, table=TABLE)


class TestDrift(unittest.TestCase):
    def test_identical_distributions_stable(self):
        d = {"bn": 600, "en": 300, "hi": 100}
        r = dr.distribution_shift(d, dict(d))
        self.assertEqual(r["status"], "stable"); self.assertAlmostEqual(r["psi"], 0.0, places=6)

    def test_big_shift_alerts_and_names_movers(self):
        r = dr.distribution_shift({"bn": 800, "en": 200}, {"bn": 300, "en": 400, "ar": 300})
        self.assertEqual(r["status"], "alert")
        self.assertIn(r["top_movers"][0]["category"], {"bn", "ar"})

    def test_new_category_does_not_crash(self):
        r = dr.distribution_shift({"a": 500}, {"a": 250, "b": 250})
        self.assertIsNotNone(r["psi"])

    def test_small_windows_give_no_verdict(self):
        self.assertEqual(dr.distribution_shift({"a": 5}, {"b": 5})["status"], "insufficient_data")

    def test_psi_needs_data(self):
        with self.assertRaises(ValueError):
            dr.psi({}, {"a": 1})

    def test_agreement_drop_detected(self):
        r = dr.agreement_drop(950, 1000, 850, 1000)
        self.assertEqual(r["status"], "degraded")

    def test_noise_is_not_degradation(self):
        self.assertEqual(dr.agreement_drop(950, 1000, 945, 1000)["status"], "ok")

    def test_improvement_is_ok(self):
        self.assertEqual(dr.agreement_drop(850, 1000, 950, 1000)["status"], "ok")

    def test_tiny_but_significant_drop_ignored(self):
        self.assertEqual(dr.agreement_drop(9900, 10000, 9860, 10000)["status"], "ok")  # 0.4 pt

    def test_insufficient_and_invalid(self):
        self.assertEqual(dr.agreement_drop(9, 10, 5, 10)["status"], "insufficient_data")
        with self.assertRaises(ValueError):
            dr.agreement_drop(11, 10, 5, 200)

    def test_perfect_windows_no_division_by_zero(self):
        self.assertEqual(dr.agreement_drop(1000, 1000, 1000, 1000)["status"], "ok")

    def test_flags_honest(self):
        self.assertFalse(dr.THRESHOLDS_CALIBRATED)


if __name__ == "__main__":
    unittest.main()
