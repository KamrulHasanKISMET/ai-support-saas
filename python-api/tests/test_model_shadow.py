"""Tests for app/language/model_shadow.py (Phase 6 shadow runner). Fake DB/embedder.
    docker compose exec python-api python -m unittest tests.test_model_shadow -v
"""
import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from tests import _sa_stub  # noqa: F401
from app.language import model_shadow as ms
from app.language.intent_model import IntentModel
from tests.test_intent_model import blobs

X, Y = blobs(dim=8)
MODEL = IntentModel().fit(X, Y)


class DB:
    def __init__(self, row=None, fail_insert=False):
        self.row, self.fail_insert = row, fail_insert
        self.inserts, self.commits, self.rollbacks, self.selects = [], 0, 0, 0

    async def execute(self, q, params=None):
        if "INSERT" in str(q):
            if self.fail_insert:
                raise RuntimeError("db down")
            self.inserts.append(params)
            return SimpleNamespace()
        self.selects += 1
        row = self.row
        return SimpleNamespace(first=lambda: row)

    async def commit(self): self.commits += 1
    async def rollback(self): self.rollbacks += 1


def shadow_row(emb="emb"):
    return SimpleNamespace(version="v1", artifact=MODEL.to_dict(), embedding_model=emb)


async def embed(texts):
    return [X[0].tolist() for _ in texts]


def go(db, **kw):
    args = dict(enabled=True, tenant_id=1, conversation_id=5, experience_id="abc", text="hello",
                served_intent="A", served_source="llm", language="bn", embed_fn=embed, embedding_model="emb")
    args.update(kw)
    return asyncio.run(ms.run_shadow(db, **args))


class TestRunShadow(unittest.TestCase):
    def setUp(self):
        ms.clear_cache()

    def test_disabled_does_nothing_not_even_a_query(self):
        db = DB(shadow_row()); self.assertIsNone(go(db, enabled=False)); self.assertEqual(db.selects, 0)

    def test_no_shadow_model_is_cheap_noop_and_cached(self):
        db = DB(None)
        self.assertIsNone(go(db)); self.assertIsNone(go(db))
        self.assertEqual(db.selects, 1)  # None cached
        self.assertEqual(db.inserts, [])

    def test_records_prediction_with_metadata_only(self):
        db = DB(shadow_row()); p = go(db)
        self.assertEqual(p.predicted_intent, "A")
        self.assertTrue(p.agrees)
        self.assertEqual(len(db.inserts), 1)
        row = db.inserts[0]
        self.assertEqual(row["t"], 1)
        self.assertNotIn("hello", json.dumps(row))          # no message text stored
        self.assertEqual(db.commits, 1)

    def test_disagreement_recorded(self):
        p = go(DB(shadow_row()), served_intent="B")
        self.assertFalse(p.agrees)

    def test_unknown_served_intent_gives_null_agrees(self):
        p = go(DB(shadow_row()), served_intent=None)
        self.assertIsNone(p.agrees)

    def test_embedding_model_mismatch_skips(self):
        db = DB(shadow_row(emb="other")); self.assertIsNone(go(db)); self.assertEqual(db.inserts, [])

    def test_empty_text_skips(self):
        self.assertIsNone(go(DB(shadow_row()), text="  "))

    def test_insert_failure_is_swallowed_and_rolled_back(self):
        db = DB(shadow_row(), fail_insert=True)
        self.assertIsNone(go(db)); self.assertEqual(db.rollbacks, 1)

    def test_embedder_failure_is_swallowed(self):
        async def boom(t): raise RuntimeError("provider")
        self.assertIsNone(go(DB(shadow_row()), embed_fn=boom))

    def test_corrupt_artifact_is_swallowed(self):
        bad = SimpleNamespace(version="v", artifact={"kind": "x"}, embedding_model="emb")
        self.assertIsNone(go(DB(bad)))

    def test_wrong_dimension_vector_is_swallowed(self):
        async def wrong(t): return [[0.0] * 3]
        self.assertIsNone(go(DB(shadow_row()), embed_fn=wrong))

    def test_query_only_reads_shadow_status_for_the_tenant(self):
        seen = []
        class Spy(DB):
            async def execute(s, q, params=None):
                seen.append((str(q), params)); return await super().execute(q, params)
        go(Spy(shadow_row()))
        sql, p = seen[0]
        self.assertIn("status = 'shadow'", sql); self.assertEqual(p["t"], 1)


class TestWiring(unittest.TestCase):
    def test_default_off_and_core_agent_guarded(self):
        cfg = (Path(ms.__file__).parents[1] / "core/config.py").read_text()
        self.assertIn("model_shadow_enabled: bool = False", cfg)
        src = (Path(ms.__file__).parents[1] / "agent/core_agent.py").read_text()
        self.assertIn("if settings.model_shadow_enabled:", src)

    def test_kernel_and_routing_do_not_import_it(self):
        root = Path(ms.__file__).parents[1]
        for f in ("kernel/kernel.py", "language/routing_service.py"):
            self.assertNotIn("model_shadow", (root / f).read_text(encoding="utf-8"))

    def test_migration_has_no_text_columns_and_90_days(self):
        sql = (Path(ms.__file__).parents[3] / "db/init/021_model_shadow.sql").read_text()
        self.assertIn("INTERVAL '90 days'", sql)
        self.assertEqual(ms.RETENTION_DAYS, 90)
        code = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
        for col in ("message", "text", "normalized"):
            self.assertNotIn(col, code.lower())


def rows(n, agree_rate, conf=0.95, lang="bn"):
    k = int(n * agree_rate)
    return [{"confidence": conf, "agrees": i < k, "language": lang} for i in range(n)]


class TestReaders(unittest.TestCase):
    def test_summary_counts_and_unscored_excluded(self):
        s = ms.summarize(rows(10, 0.8) + [{"confidence": 0.9, "agrees": None, "language": "bn"}])
        self.assertEqual((s["total"], s["scored"], s["unscored"]), (11, 10, 1))
        self.assertAlmostEqual(s["agreement"], 0.8)

    def test_empty_is_safe(self):
        s = ms.summarize([]); self.assertIsNone(s["agreement"])
        self.assertEqual(ms.ready_for_canary(s)[0].split(":")[0], "too_few_samples")

    def test_ready_when_enough_and_good(self):
        self.assertEqual(ms.ready_for_canary(ms.summarize(rows(300, 0.97))), [])

    def test_low_agreement_blocks(self):
        self.assertTrue(any(f.startswith("agreement") for f in ms.ready_for_canary(ms.summarize(rows(300, 0.7)))))

    def test_one_bad_language_blocks_even_if_overall_good(self):
        r = rows(400, 0.99) + rows(40, 0.3, lang="hi")
        f = ms.ready_for_canary(ms.summarize(r))
        self.assertTrue(any(x.startswith("language_slice:hi") for x in f))

    def test_tiny_language_slice_not_gated(self):
        r = rows(300, 0.99) + rows(3, 0.0, lang="xx")
        self.assertEqual(ms.ready_for_canary(ms.summarize(r)), [])

    def test_thresholds_flag_honest(self):
        self.assertFalse(ms.THRESHOLDS_CALIBRATED)


if __name__ == "__main__":
    unittest.main()


class TestPurgeAndCLI(unittest.TestCase):
    def test_purge_expired_deletes_and_commits(self):
        class PurgeDB(DB):
            async def execute(self, q, params=None):
                if "DELETE FROM model_shadow_predictions" in str(q):
                    return SimpleNamespace(rowcount=7)
                return await super().execute(q, params)
        db = PurgeDB()
        n = asyncio.run(ms.purge_expired(db))
        self.assertEqual(n, 7)
        self.assertEqual(db.commits, 1)

    def test_purge_sql_uses_expires_at(self):
        self.assertIn("expires_at < CURRENT_TIMESTAMP", ms.PURGE_SQL)

    def test_cli_module_has_purge_and_report_paths(self):
        src = Path(ms.__file__).read_text()
        self.assertIn("--purge", src)
        self.assertIn("report_for", src)
