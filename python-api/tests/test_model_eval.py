"""Tests for app/language/model_eval.py (Phase 6). Synthetic.
    docker compose exec python-api python -m unittest tests.test_model_eval -v
"""
import unittest

import numpy as np

from app.language import model_eval as me
from app.language.intent_model import IntentModel
from tests.test_intent_model import blobs


def setup(noise=0.5, n_te=40):
    Xtr, ytr = blobs(noise=noise, seed=0, n_per=60)
    m = IntentModel().fit(Xtr, ytr)
    Xte, yte = blobs(noise=noise, seed=0, n_per=n_te)
    Xte = Xte + np.random.default_rng(8).normal(scale=0.2, size=Xte.shape)
    return m, Xtr, ytr, Xte, yte


class TestEvaluate(unittest.TestCase):
    def test_good_model_passes(self):
        m, Xtr, ytr, Xte, yte = setup()
        r = me.evaluate(m, Xte, yte, ["bn"] * len(yte), Xtr=Xtr, ytr=ytr)
        self.assertGreater(r.accuracy, 0.9)
        self.assertGreaterEqual(r.n, me.MIN_TEST_EXAMPLES)
        self.assertEqual(r.thresholds_calibrated, False)

    def test_tiny_test_set_is_not_evidence(self):
        m, Xtr, ytr, Xte, yte = setup(n_te=5)
        r = me.evaluate(m, Xte, yte, ["bn"] * len(yte), Xtr=Xtr, ytr=ytr)
        self.assertFalse(r.ok)
        self.assertTrue(r.failures[0].startswith("test_set_too_small"))

    def test_one_broken_language_slice_fails_the_gate(self):
        m, Xtr, ytr, Xte, yte = setup(n_te=60)
        langs = ["bn"] * len(yte)
        # 'hi' slice: random garbage features -> model must fail there
        idx = list(range(0, len(yte), 4))
        Xte = Xte.copy()
        Xte[idx] = np.random.default_rng(1).normal(size=(len(idx), Xte.shape[1]))
        for i in idx:
            langs[i] = "hi"
        r = me.evaluate(m, Xte, yte, langs, Xtr=Xtr, ytr=ytr)
        self.assertTrue(any(f.startswith("language_slice:hi") for f in r.failures))

    def test_unseen_label_counts_as_error(self):
        m, Xtr, ytr, Xte, yte = setup()
        yte2 = ["NEVER_SEEN"] * len(yte)
        r = me.evaluate(m, Xte, yte2, ["bn"] * len(yte), Xtr=Xtr, ytr=ytr)
        self.assertEqual(r.accuracy, 0.0)

    def test_length_mismatch_is_loud(self):
        m, Xtr, ytr, Xte, yte = setup()
        with self.assertRaises(ValueError):
            me.evaluate(m, Xte, yte, ["bn"], Xtr=Xtr, ytr=ytr)

    def test_report_is_json_safe(self):
        import json
        m, Xtr, ytr, Xte, yte = setup()
        json.dumps(me.report_to_dict(me.evaluate(m, Xte, yte, ["bn"] * len(yte), Xtr=Xtr, ytr=ytr)))


class TestMetrics(unittest.TestCase):
    def test_ece_zero_when_calibrated(self):
        self.assertAlmostEqual(me.expected_calibration_error([1.0] * 10, [True] * 10), 0.0)

    def test_ece_high_when_overconfident(self):
        self.assertAlmostEqual(me.expected_calibration_error([0.99] * 10, [False] * 10), 0.99, places=2)

    def test_ece_empty_safe(self):
        self.assertEqual(me.expected_calibration_error([], []), 0.0)

    def test_macro_f1_exact(self):
        f1, per = me._macro_f1(["a", "a", "b", "b"], ["a", "b", "b", "b"])
        self.assertAlmostEqual(per["a"], 2 / 3)
        self.assertAlmostEqual(per["b"], 0.8)
        self.assertAlmostEqual(f1, (2 / 3 + 0.8) / 2)

    def test_baseline_is_nearest_centroid(self):
        Xtr = np.array([[1, 0], [1, 0.1], [0, 1], [0.1, 1]])
        self.assertEqual(me.nearest_centroid_predict(Xtr, ["a", "a", "b", "b"], np.array([[2, 0.1], [0.1, 3]])), ["a", "b"])


class TestGate(unittest.TestCase):
    def rep(self, **kw):
        base = dict(n=100, accuracy=0.95, macro_f1=0.9, ece=0.05, coverage=0.6, selective_accuracy=0.98,
                    baseline_accuracy=0.90, per_language={"bn": {"n": 50, "accuracy": 0.95}})
        base.update(kw)
        return me.EvalReport(**base)

    def test_pass(self):
        self.assertEqual(me.gate_failures(self.rep()), [])

    def test_each_rule_names_itself(self):
        cases = {"accuracy": dict(accuracy=0.5, baseline_accuracy=0.1),
                 "not_better_than_baseline": dict(baseline_accuracy=0.95),
                 "calibration": dict(ece=0.3),
                 "selective_accuracy": dict(selective_accuracy=0.7)}
        for name, kw in cases.items():
            self.assertTrue(any(f.startswith(name) for f in me.gate_failures(self.rep(**kw))), name)

    def test_small_language_slice_reported_not_gated(self):
        r = self.rep(per_language={"xx": {"n": 3, "accuracy": 0.0}})
        self.assertEqual(me.gate_failures(r), [])

    def test_answers_nothing_is_not_a_failure_by_itself(self):
        self.assertEqual(me.gate_failures(self.rep(selective_accuracy=None, coverage=0.0)), [])

    def test_thresholds_flag_honest(self):
        self.assertFalse(me.THRESHOLDS_CALIBRATED)


if __name__ == "__main__":
    unittest.main()
