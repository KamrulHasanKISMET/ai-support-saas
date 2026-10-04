"""Tests for app/language/intent_model.py (Phase 6). Synthetic, deterministic.
    docker compose exec python-api python -m unittest tests.test_intent_model -v
"""
import json
import unittest

import numpy as np

from app.language.intent_model import IntentModel, ModelError


def blobs(n_per=40, dim=16, labels=("A", "B", "C"), noise=0.6, seed=0):
    rng = np.random.default_rng(seed)
    centers = {l: rng.normal(size=dim) for l in labels}
    X, y = [], []
    for l in labels:
        for _ in range(n_per):
            X.append(centers[l] + rng.normal(scale=noise, size=dim)); y.append(l)
    return np.array(X), y


class TestFit(unittest.TestCase):
    def test_learns_separable_classes(self):
        X, y = blobs()
        m = IntentModel().fit(X, y)
        acc = np.mean([p == t for (p, _), t in zip(m.predict(X), y)])
        self.assertGreater(acc, 0.97)

    def test_generalises_to_fresh_samples(self):
        m = IntentModel().fit(*blobs(seed=0))
        Xn, yn = blobs(seed=0, n_per=20)  # same centers (seed), different draws below
        rng = np.random.default_rng(99)
        Xn = Xn + rng.normal(scale=0.3, size=Xn.shape)
        acc = np.mean([p == t for (p, _), t in zip(m.predict(Xn), yn)])
        self.assertGreater(acc, 0.9)

    def test_deterministic(self):
        X, y = blobs()
        a, b = IntentModel().fit(X, y), IntentModel().fit(X, y)
        self.assertEqual(a.fingerprint(), b.fingerprint())

    def test_probabilities_sum_to_one(self):
        X, y = blobs()
        P = IntentModel().fit(X, y).predict_proba(X[:5])
        np.testing.assert_allclose(P.sum(axis=1), 1.0, atol=1e-9)

    def test_rare_class_is_not_drowned_out(self):
        X, y = blobs(n_per=60, labels=("A", "B"))
        rng = np.random.default_rng(3)
        rare = rng.normal(size=16) * 2
        Xr = rare + rng.normal(scale=0.3, size=(6, 16))
        m = IntentModel().fit(np.vstack([X, Xr]), y + ["RARE"] * 6)
        got = [p for p, _ in m.predict(Xr)]
        self.assertGreaterEqual(got.count("RARE"), 5)

    def test_scale_invariant_inputs(self):
        X, y = blobs()
        m = IntentModel().fit(X, y)
        self.assertEqual([p for p, _ in m.predict(X[:10])], [p for p, _ in m.predict(X[:10] * 7.5)])


class TestErrors(unittest.TestCase):
    def test_bad_training_inputs_are_loud(self):
        X, y = blobs()
        for args in ((X[:0], []), (X, y[:-1]), (X, ["A"] * len(y))):
            with self.assertRaises(ModelError):
                IntentModel().fit(*args)
        bad = X.copy(); bad[0, 0] = np.nan
        with self.assertRaises(ModelError):
            IntentModel().fit(bad, y)

    def test_unfitted_and_wrong_dim(self):
        with self.assertRaises(ModelError):
            IntentModel().predict_proba(np.zeros((1, 4)))
        m = IntentModel().fit(*blobs())
        with self.assertRaises(ModelError):
            m.predict_proba(np.zeros((1, 5)))


class TestCalibration(unittest.TestCase):
    def test_calibration_never_worsens_validation_nll(self):
        Xtr, ytr = blobs(noise=1.4, seed=1)
        m = IntentModel(epochs=600, lr=1.0, l2=1e-6).fit(Xtr, ytr)
        Xv, yv = blobs(noise=1.4, seed=2, n_per=40)

        def nll():
            P = m.predict_proba(Xv)
            t = [m.labels.index(l) for l in yv]
            return float(-np.log(np.clip(P[np.arange(len(t)), t], 1e-12, 1)).mean())

        before = nll()
        T = m.calibrate_temperature(Xv, yv)
        self.assertLessEqual(nll(), before + 1e-9)
        self.assertTrue(0.5 <= T <= 5.0)

    def test_unseen_validation_labels_are_skipped(self):
        X, y = blobs()
        m = IntentModel().fit(X, y)
        self.assertEqual(m.calibrate_temperature(X[:1], ["ZZZ"]), 1.0)


class TestSerialisation(unittest.TestCase):
    def test_round_trip_identical_predictions(self):
        X, y = blobs()
        m = IntentModel().fit(X, y)
        m.calibrate_temperature(X, y)
        m2 = IntentModel.from_dict(json.loads(json.dumps(m.to_dict())))
        np.testing.assert_allclose(m.predict_proba(X[:8]), m2.predict_proba(X[:8]), atol=1e-5)

    def test_artifact_has_no_text_or_person_fields(self):
        d = IntentModel().fit(*blobs()).to_dict()
        self.assertEqual(set(d), {"kind", "labels", "dim", "temperature", "trained_on", "W", "b"})

    def test_malformed_artifacts_rejected(self):
        good = IntentModel().fit(*blobs()).to_dict()
        for mut in (lambda d: d.update(kind="x"), lambda d: d.update(dim=3),
                    lambda d: d.update(temperature=0), lambda d: d.pop("W"),
                    lambda d: d["b"].__setitem__(0, float("nan"))):
            d = json.loads(json.dumps(good, allow_nan=True)); mut(d)
            with self.assertRaises(ModelError):
                IntentModel.from_dict(d)
        with self.assertRaises(ModelError):
            IntentModel.from_dict("nope")


if __name__ == "__main__":
    unittest.main()
