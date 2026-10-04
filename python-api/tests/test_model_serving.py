"""Tests for app/language/model_serving.py (Phase 6 P6-4). Fakes only.
    docker compose exec python-api python -m unittest tests.test_model_serving -v
"""
import asyncio
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from tests import _sa_stub  # noqa: F401
from app.language import model_serving as ms
from app.language.intent_model import IntentModel
from tests.test_intent_model import blobs

LABELS = ("PRICE_INQUIRY", "DELIVERY_INFO", "CREATE_ORDER")
X, Y = blobs(dim=16, labels=LABELS)
MODEL = IntentModel().fit(X, Y)
VEC = X[0].tolist()                     # a PRICE_INQUIRY-like vector
GOOD_F1 = {l: 0.95 for l in LABELS}
OK_REPORT = {"ok": True, "per_intent_f1": GOOD_F1}
ROOT = Path(__file__).resolve().parents[2]


def lm(status="active", report=None, emb="emb", pct=None, model=MODEL):
    return ms.LoadedModel("v1", model, emb, OK_REPORT if report is None else report, status, pct)


def ev(loaded, vec=VEC, **kw):
    kw.setdefault("embedding_model", "emb")
    kw.setdefault("audit_rng", lambda: 0.99)     # not audited unless a test says so
    return ms.evaluate(loaded, vec, **kw)


class TestEvaluateRules(unittest.TestCase):
    def test_active_confident_model_serves_with_hint(self):
        d = ev(lm())
        self.assertTrue(d.serve, d.reason)
        self.assertEqual(d.intent, "PRICE_INQUIRY")
        self.assertTrue(d.use_hint)
        self.assertFalse(d.audited)

    def test_canary_model_never_serves_by_default(self):
        d = ev(lm("canary", pct=100.0))
        self.assertFalse(d.serve)
        self.assertEqual(d.reason, "canary_serving_disabled")

    def test_canary_serves_only_inside_percentage(self):
        inside = ev(lm("canary", pct=10.0), canary_enabled=True, rng=lambda: 0.05)
        outside = ev(lm("canary", pct=10.0), canary_enabled=True, rng=lambda: 0.50)
        self.assertTrue(inside.serve)
        self.assertEqual(outside.reason, "canary_not_selected")

    def test_canary_without_pct_never_serves(self):
        self.assertFalse(ev(lm("canary", pct=None), canary_enabled=True).serve)

    def test_shadow_trained_retired_models_never_serve(self):
        for st in ("shadow", "trained", "retired", "rejected"):
            d = ev(lm(st), canary_enabled=True)
            self.assertFalse(d.serve, st)

    def test_failed_offline_eval_blocks(self):
        self.assertEqual(ev(lm(report={"ok": False, "per_intent_f1": GOOD_F1})).reason,
                         "offline_eval_not_passed")
        self.assertEqual(ev(lm(report={})).reason, "offline_eval_not_passed")

    def test_embedding_model_mismatch_blocks(self):
        self.assertEqual(ev(lm(emb="other")).reason, "embedding_model_mismatch")

    def test_low_confidence_falls_back(self):
        d = ev(lm(), vec=(np.array(X[0]) + np.array(X[45])).tolist())  # ambiguous mix
        if d.serve:                                   # blobs may still separate; force it
            old = ms.SERVE_MIN_CONFIDENCE
            ms.SERVE_MIN_CONFIDENCE = 1.01
            try:
                d = ev(lm())
            finally:
                ms.SERVE_MIN_CONFIDENCE = old
        self.assertFalse(d.serve)
        self.assertEqual(d.reason, "low_confidence")

    def test_unknown_intent_label_never_served(self):
        X2, y2 = blobs(dim=16, labels=("A", "B"))
        m2 = IntentModel().fit(X2, y2)
        d = ev(lm(model=m2, report={"ok": True, "per_intent_f1": {"A": 1.0, "B": 1.0}}),
               vec=X2[0].tolist())
        self.assertFalse(d.serve)
        self.assertEqual(d.reason, "unknown_intent")

    def test_intent_without_evidence_never_served(self):
        d = ev(lm(report={"ok": True, "per_intent_f1": {"DELIVERY_INFO": 0.99}}))
        self.assertEqual(d.reason, "no_intent_evidence")

    def test_intent_below_per_intent_floor_never_served(self):
        f1 = dict(GOOD_F1, PRICE_INQUIRY=ms.MIN_PER_INTENT_F1 - 0.01)
        self.assertEqual(ev(lm(report={"ok": True, "per_intent_f1": f1})).reason,
                         "intent_below_floor")

    def test_floor_boundary_is_inclusive(self):
        f1 = dict(GOOD_F1, PRICE_INQUIRY=ms.MIN_PER_INTENT_F1)
        self.assertTrue(ev(lm(report={"ok": True, "per_intent_f1": f1})).serve)

    def test_wrong_dimension_raises_for_caller_to_isolate(self):
        with self.assertRaises(Exception):
            ev(lm(), vec=[0.1, 0.2])


class TestAuditSampling(unittest.TestCase):
    def test_audited_turn_does_not_inject_hint(self):
        d = ev(lm(), audit_rng=lambda: 0.0)
        self.assertTrue(d.serve and d.audited)
        self.assertFalse(d.use_hint)              # LLM still classifies it

    def test_rate_is_a_minority_placeholder(self):
        self.assertGreater(ms.AUDIT_SAMPLE_RATE, 0)
        self.assertLess(ms.AUDIT_SAMPLE_RATE, 0.5)
        self.assertFalse(ms.THRESHOLDS_CALIBRATED)


class TestActionSafety(unittest.TestCase):
    def test_create_order_served_but_flagged_not_automatable(self):
        X3 = np.array(X)
        d = ev(lm(), vec=X3[85].tolist())          # CREATE_ORDER block (sorted labels)
        # whichever intent the model picks, the flag must mirror control_plane
        from app.language import control_plane
        self.assertEqual(d.automation_eligible, control_plane.is_automation_eligible(d.intent))
        self.assertFalse(control_plane.is_automation_eligible("CREATE_ORDER"))
        self.assertFalse(control_plane.is_automation_eligible("ORDER_STATUS"))


class FakeDB:
    def __init__(self, row=None, fail=False):
        self.row, self.fail = row, fail
        self.selects, self.inserts, self.commits, self.rollbacks = 0, [], 0, 0

    async def execute(self, q, params=None):
        if "INSERT" in str(q):
            if self.fail:
                raise RuntimeError("db down")
            self.inserts.append(params)
            return SimpleNamespace()
        self.selects += 1
        return SimpleNamespace(first=lambda: self.row)

    async def commit(self): self.commits += 1
    async def rollback(self): self.rollbacks += 1


def row(status="active", pct=None):
    return SimpleNamespace(version="v1", artifact=MODEL.to_dict(), embedding_model="emb",
                           eval_report=OK_REPORT, status=status, canary_pct=pct)


async def embed(texts):
    return [VEC for _ in texts]


def decide(db, **kw):
    args = dict(enabled=True, canary_enabled=False, tenant_id=1, text="dam koto",
                embed_fn=embed, embedding_model="emb", audit_rng=lambda: 0.99)
    args.update(kw)
    return asyncio.run(ms.decide(db, **args))


class TestDecide(unittest.TestCase):
    def setUp(self):
        ms.clear_cache()

    def test_flag_off_does_nothing_not_even_a_query(self):
        db = FakeDB(row())
        self.assertIsNone(decide(db, enabled=False))
        self.assertEqual(db.selects, 0)

    def test_blank_text_does_nothing(self):
        db = FakeDB(row())
        self.assertIsNone(decide(db, text="   "))
        self.assertEqual(db.selects, 0)

    def test_no_model_is_cheap_noop_and_cached(self):
        db = FakeDB(None)
        self.assertIsNone(decide(db))
        self.assertIsNone(decide(db))
        self.assertEqual(db.selects, 1)

    def test_active_model_serves(self):
        d = decide(FakeDB(row()))
        self.assertTrue(d.serve)
        self.assertEqual(d.status, "active")

    def test_embedding_failure_is_isolated(self):
        async def boom(_): raise RuntimeError("no embedding provider")
        self.assertIsNone(decide(FakeDB(row()), embed_fn=boom))

    def test_corrupt_artifact_is_isolated(self):
        bad = SimpleNamespace(version="v", artifact={"kind": "nope"}, embedding_model="emb",
                              eval_report=OK_REPORT, status="active", canary_pct=None)
        self.assertIsNone(decide(FakeDB(bad)))

    def test_db_failure_is_isolated(self):
        class Down(FakeDB):
            async def execute(self, q, params=None): raise RuntimeError("db down")
        self.assertIsNone(decide(Down()))

    def test_cache_ttl_lets_a_retire_take_effect(self):
        clock = [0.0]
        db = FakeDB(row())
        asyncio.run(ms.load_serving_model(db, 1, now=lambda: clock[0]))
        clock[0] = ms.CACHE_TTL_SECONDS + 1
        asyncio.run(ms.load_serving_model(db, 1, now=lambda: clock[0]))
        self.assertEqual(db.selects, 2)


def serve_decision(audited=False):
    return ms.ServeDecision(True, "ok", "v1", "active", "PRICE_INQUIRY", 0.97,
                            not audited, audited, None, True)


def record(db, decision, final="PRICE_INQUIRY", used_hint=True):
    return asyncio.run(ms.record_served(
        db, tenant_id=1, conversation_id=5, experience_id="abc", decision=decision,
        final_intent=final, kernel_used_hint=used_hint, language="bn"))


class TestRecord(unittest.TestCase):
    def test_non_audited_turn_never_claims_agreement(self):
        db = FakeDB()
        self.assertTrue(record(db, serve_decision(False)))
        p = db.inserts[0]
        self.assertIsNone(p["ag"])                 # would be a tautology
        self.assertTrue(p["hu"])

    def test_audited_turn_records_real_agreement_and_disagreement(self):
        db = FakeDB()
        record(db, serve_decision(True), final="PRICE_INQUIRY", used_hint=False)
        record(db, serve_decision(True), final="DELIVERY_INFO", used_hint=False)
        self.assertEqual([p["ag"] for p in db.inserts], [True, False])
        self.assertFalse(db.inserts[0]["hu"])

    def test_rejected_hint_is_recorded_as_not_used(self):
        db = FakeDB()
        record(db, serve_decision(False), used_hint=False)
        self.assertFalse(db.inserts[0]["hu"])

    def test_declined_decision_writes_nothing(self):
        db = FakeDB()
        d = ms.ServeDecision(False, "low_confidence", "v1", "active", "PRICE_INQUIRY",
                             0.5, False, False, None, True)
        self.assertFalse(record(db, d))
        self.assertFalse(record(db, None))
        self.assertEqual(db.inserts, [])

    def test_no_message_text_in_written_params(self):
        db = FakeDB()
        record(db, serve_decision(False))
        for bad in ("text", "message", "normalized", "original", "customer_id", "phone", "name"):
            self.assertFalse([k for k in db.inserts[0] if bad in k], bad)

    def test_write_failure_is_isolated_and_rolled_back(self):
        db = FakeDB(fail=True)
        self.assertFalse(record(db, serve_decision(False)))
        self.assertEqual(db.rollbacks, 1)


class TestSummarize(unittest.TestCase):
    def test_accuracy_uses_audited_rows_only(self):
        rows = [{"audited": False, "agrees": None, "hint_used": True, "language": "bn"}] * 50 + [
            {"audited": True, "agrees": True, "hint_used": False, "language": "bn"},
            {"audited": True, "agrees": False, "hint_used": False, "language": "hi"},
        ]
        s = ms.summarize_served(rows)
        self.assertEqual((s["served"], s["audited"], s["hint_used"]), (52, 2, 50))
        self.assertEqual(s["audited_agreement"], 0.5)
        self.assertEqual(s["per_language"]["bn"]["agreement"], 1.0)

    def test_no_audited_rows_means_unknown_not_perfect(self):
        s = ms.summarize_served([{"audited": False, "agrees": None, "hint_used": True, "language": "bn"}])
        self.assertIsNone(s["audited_agreement"])


class TestFilesAgree(unittest.TestCase):
    def test_migration_matches_code_retention_and_columns(self):
        sql = (ROOT / "db/init/024_model_serving.sql").read_text()
        self.assertIn(f"INTERVAL '{ms.RETENTION_DAYS} days'", sql)
        self.assertIn("agrees IS NULL OR audited = TRUE", sql)
        for col in ("hint_used", "audited", "agrees", "automation_eligible", "model_intent"):
            self.assertIn(col, sql)
        for banned in ("message_text", "normalized_message", "original_message", "customer_id"):
            self.assertNotIn(banned, sql.split("CREATE TABLE")[1])
        self.assertIn("024", (ROOT / "scripts/migrate.sh").read_text())

    def test_insert_columns_exist_in_migration(self):
        sql = (ROOT / "db/init/024_model_serving.sql").read_text()
        src = Path(ms.__file__).read_text()
        cols = re.search(r"INSERT INTO model_served_turns\s*\(([^)]*)\)", src, re.S).group(1)
        for c in [x.strip() for x in cols.split(",")]:
            self.assertIn(c, sql, c)

    def test_flags_default_off(self):
        cfg = (ROOT / "python-api/app/core/config.py").read_text()
        self.assertRegex(cfg, r"model_serving_enabled:\s*bool\s*=\s*False")
        self.assertRegex(cfg, r"model_serving_canary_enabled:\s*bool\s*=\s*False")

    def test_core_agent_hook_is_flag_guarded_and_fallback_only(self):
        src = (ROOT / "python-api/app/agent/core_agent.py").read_text()
        self.assertIn("settings.model_serving_enabled", src)
        self.assertIn("and brain_hint is None", src)          # never overrides the cluster Brain
        self.assertIn("model_serve.use_hint", src)
        self.assertIn("kernel_used_hint=(kernel_result.intentSource", src)

    def test_request_path_modules_do_not_import_serving_except_core_agent(self):
        base = ROOT / "python-api/app"
        for rel in ("kernel/kernel.py", "language/routing_service.py", "intent/intent_engine.py"):
           self.assertNotIn("model_serving", (base / rel).read_text(encoding="utf-8"), rel)


if __name__ == "__main__":
    unittest.main()
