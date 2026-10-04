"""Tests for app/language/train_intent_model.py (Phase 6). Fake DB + fake
embedder -- the whole pipeline runs, no provider/DB.
    docker compose exec python-api python -m unittest tests.test_train_intent_model -v
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from tests import _sa_stub  # noqa: F401
from app.language import distillation_dataset as dd
from app.language import train_intent_model as t

INTENTS = ("PRICE_INQUIRY", "COMPLAINT", "ORDER_STATUS")
CENTERS = {i: np.random.default_rng(k).normal(size=24) for k, i in enumerate(INTENTS)}


def embed_fn_factory(fail=False):
    async def embed(texts):
        if fail:
            raise RuntimeError("provider down")
        out = []
        for s in texts:
            intent = s.split("|")[0]
            r = np.random.default_rng(abs(hash(s)) % (2**32))
            out.append((CENTERS[intent] + r.normal(scale=0.4, size=24)).tolist())
        return out
    return embed


def examples(n_per):
    return [dd.Example(f"{i}-{k}", i, f"{i}|utterance {k}", "bn", 1) for i in INTENTS for k in range(n_per)]


class FakeDB:
    def __init__(self):
        self.inserts, self.commits = [], 0

    async def execute(self, q, params=None):
        self.inserts.append((str(q), params))
        return SimpleNamespace()

    async def commit(self):
        self.commits += 1


def run(n_per, dry=False, fail_embed=False, db=None):
    db = db or FakeDB()

    async def go():
        with mock.patch.object(dd, "fetch_examples", mock.AsyncMock(return_value=examples(n_per))):
            return await t.train_for_tenant(db, 1, embed_fn=embed_fn_factory(fail_embed),
                                            embedding_model="emb", dry_run=dry, eval_texts=set())
    return asyncio.run(go()), db


class TestPipeline(unittest.TestCase):
    def test_insufficient_data_stops_with_reason_and_writes_nothing(self):
        out, db = run(5)
        self.assertEqual(out.status, "insufficient_data")
        self.assertIn("need", out.detail)
        self.assertEqual(db.inserts, [])

    def test_dry_run_evaluates_but_writes_nothing(self):
        out, db = run(150, dry=True)
        self.assertEqual(out.status, "dry_run")
        self.assertIsNotNone(out.report)
        self.assertEqual(db.inserts, [])

    def test_full_run_registers_a_trained_model(self):
        out, db = run(150)
        self.assertEqual(out.status, "registered")
        self.assertEqual(len(db.inserts), 1)
        self.assertIn("'trained'", db.inserts[0][0])
        self.assertGreaterEqual(out.report["accuracy"], 0.95)
        # Synthetic classes are so easy the centroid baseline is also perfect,
        # so the gate correctly refuses "better than baseline": the ONLY failure.
        self.assertEqual([f.split(":")[0] for f in out.report["failures"]], ["not_better_than_baseline"])
        self.assertEqual(db.inserts[0][1]["tenant_id"], 1)

    def test_model_that_beats_baseline_and_meets_bar_passes(self):
        with mock.patch.object(t.model_eval, "MIN_GAIN_OVER_BASELINE", -1.0):
            out, _ = run(150)
        self.assertTrue(out.report["ok"], out.report["failures"])

    def test_failed_gate_is_still_registered_but_marked_failed(self):
        with mock.patch.object(t.model_eval, "MIN_ACCURACY", 1.01):
            out, db = run(150)
        self.assertEqual(out.status, "registered")
        self.assertFalse(out.report["ok"])
        self.assertIn("FAILED", out.detail)

    def test_embedding_failure_is_an_error_outcome_not_an_exception(self):
        out, db = run(150, fail_embed=True)
        self.assertEqual(out.status, "error")
        self.assertEqual(db.inserts, [])

    def test_fetch_failure_never_raises(self):
        async def go():
            with mock.patch.object(dd, "fetch_examples", mock.AsyncMock(side_effect=RuntimeError("db"))):
                return await t.train_for_tenant(FakeDB(), 1, embed_fn=embed_fn_factory(), embedding_model="e")
        self.assertEqual(asyncio.run(go()).status, "error")

    def test_cross_tenant_row_becomes_error_and_nothing_written(self):
        bad = examples(50) + [dd.Example("x", "COMPLAINT", "COMPLAINT|leak", "bn", 999)]
        db = FakeDB()

        async def go():
            with mock.patch.object(dd, "fetch_examples", mock.AsyncMock(return_value=bad)):
                return await t.train_for_tenant(db, 1, embed_fn=embed_fn_factory(), embedding_model="e", eval_texts=set())
        out = asyncio.run(go())
        self.assertEqual(out.status, "error")
        self.assertIn("cross-tenant", out.detail)
        self.assertEqual(db.inserts, [])

    def test_never_touches_serving_paths(self):
        from pathlib import Path
        root = Path(__file__).parents[1] / "app"
        for f in ("kernel/kernel.py", "agent/core_agent.py", "language/routing_service.py"):
            src = (root / f).read_text(encoding="utf-8")
            for mod in ("intent_model", "model_registry", "train_intent_model"):
                self.assertNotIn(mod, src, f"{f} must not import {mod} yet")


if __name__ == "__main__":
    unittest.main()
