"""Tests for app/language/distillation_dataset.py (Phase 6).
    docker compose exec python-api python -m unittest tests.test_distillation_dataset -v
"""
import re
import unittest
from pathlib import Path

from app.language import distillation_dataset as dd
from app.language.distillation_dataset import Example, build_dataset, split_of


def ex(i, intent="PRICE_INQUIRY", text=None, tenant=1, lang="bn"):
    return Example(f"e{i}", intent, text if text is not None else f"sentence number {i}", lang, tenant)


def many(n, intent="PRICE_INQUIRY", start=0, tenant=1):
    return [ex(start + i, intent, tenant=tenant) for i in range(n)]


class TestSplit(unittest.TestCase):
    def test_deterministic_and_roughly_70_15_15(self):
        rows = many(2000)
        a, b = build_dataset(1, rows, eval_texts=set()), build_dataset(1, rows, eval_texts=set())
        self.assertEqual(a.fingerprint, b.fingerprint)
        c = a.counts()
        self.assertAlmostEqual(c["train"] / 2000, 0.70, delta=0.04)
        self.assertAlmostEqual(c["val"] / 2000, 0.15, delta=0.03)
        self.assertAlmostEqual(c["test"] / 2000, 0.15, delta=0.03)

    def test_adding_rows_never_moves_an_old_row(self):
        old = {e.experience_id: split_of(1, e.experience_id) for e in many(200)}
        ds = build_dataset(1, many(200) + many(200, start=1000), eval_texts=set())
        got = {e.experience_id: s for s, part in (("train", ds.train), ("val", ds.val), ("test", ds.test)) for e in part}
        for k, v in old.items():
            self.assertEqual(got[k], v)

    def test_splits_are_disjoint(self):
        ds = build_dataset(1, many(500), eval_texts=set())
        ids = [e.experience_id for p in (ds.train, ds.val, ds.test) for e in p]
        self.assertEqual(len(ids), len(set(ids)))

    def test_split_depends_on_tenant(self):
        same = sum(split_of(1, f"e{i}") == split_of(2, f"e{i}") for i in range(300))
        self.assertLess(same, 300)


class TestGuards(unittest.TestCase):
    def test_cross_tenant_row_refused_loudly(self):
        rows = many(20) + [ex(99, tenant=2)]
        with self.assertRaises(ValueError):
            build_dataset(1, rows, eval_texts=set())

    def test_eval_set_text_never_reaches_training(self):
        leak = "Price of this?"
        ds = build_dataset(1, many(30) + [ex(500, text="  price OF this? ")], eval_texts={dd._norm(leak)})
        self.assertEqual(ds.excluded.get("eval_set_leak"), 1)
        self.assertNotIn("price of this?", [dd._norm(e.text) for p in (ds.train, ds.val, ds.test) for e in p])

    def test_duplicates_collapse_case_and_space_insensitively(self):
        rows = many(20) + [ex(600, text="Hello  World"), ex(601, text="hello world")]
        ds = build_dataset(1, rows, eval_texts=set())
        self.assertEqual(ds.excluded.get("duplicate"), 1)

    def test_empty_rows_dropped(self):
        ds = build_dataset(1, many(20) + [ex(700, text="   "), ex(701, intent="")], eval_texts=set())
        self.assertEqual(ds.excluded.get("empty"), 2)

    def test_small_classes_excluded_and_reported_not_trained(self):
        ds = build_dataset(1, many(40) + many(3, "COMPLAINT", start=900), eval_texts=set())
        self.assertEqual(ds.excluded_classes, {"COMPLAINT": 3})
        self.assertNotIn("COMPLAINT", ds.classes())

    def test_training_ineligible_intent_excluded(self):
        from unittest import mock
        with mock.patch.object(dd.control_plane, "is_training_eligible", lambda i: i != "NEGOTIATION"):
            ds = build_dataset(1, many(30) + many(30, "NEGOTIATION", start=800), eval_texts=set())
        self.assertEqual(ds.excluded.get("training_ineligible"), 30)

    def test_order_intents_are_trainable_language_data(self):
        ds = build_dataset(1, many(40, "ORDER_STATUS"), eval_texts=set())
        self.assertIn("ORDER_STATUS", ds.classes())  # §3: learnable even though not automatable

    def test_fingerprint_contains_no_text(self):
        ds = build_dataset(1, [ex(i, text=f"secret{i}") for i in range(30)], eval_texts=set())
        self.assertNotIn("secret", ds.fingerprint)
        self.assertEqual(len(ds.fingerprint), 64)


class TestSqlAgreement(unittest.TestCase):
    def test_gate_matches_cluster_builder(self):
        src = (Path(dd.__file__).parent / "cluster_builder_service.py").read_text()
        norm = lambda s: re.sub(r"\s+", " ", s)
        for clause in ("le.learning_eligible = TRUE", "le.superseded_by IS NULL",
                       "'outcome_positive', 'human_confirmed'",
                       "le.verification_level = 'self_consistent'",
                       "le.source_reliability >= COALESCE(tcc.min_reliability, 0.90)"):
            self.assertIn(clause, norm(src))
            self.assertIn(clause, norm(dd.ELIGIBLE_ROWS_SQL))
        self.assertIn("le.tenant_id = :tenant_id", norm(dd.ELIGIBLE_ROWS_SQL))

    def test_real_eval_set_loads(self):
        self.assertGreater(len(dd.load_eval_texts()), 100)


if __name__ == "__main__":
    unittest.main()
