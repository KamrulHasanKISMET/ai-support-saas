"""Tests for app/language/model_canary_service.py (Phase 6 P6-3). Fake DB.
    docker compose exec python-api python -m unittest tests.test_model_canary_service -v
"""
import asyncio
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from tests import _sa_stub  # noqa: F401
from app.language import model_canary_service as mc
from app.language import model_registry as mr


def row(version="v1", pct=5.0, updated_at=None, capability="intent"):
    updated_at = updated_at or (datetime.utcnow() - timedelta(hours=48))
    age = (datetime.utcnow() - updated_at).total_seconds() / 3600.0
    return SimpleNamespace(capability=capability, version=version, canary_pct=pct,
                            updated_at=updated_at, stage_age_hours=age)


def shadow_rows(n, agree_rate, conf=0.95, lang="bn"):
    k = int(n * agree_rate)
    return [SimpleNamespace(confidence=conf, agrees=(i < k), language=lang) for i in range(n)]


class FakeDB:
    def __init__(self, canaries, shadow_by_version, cfg=None):
        self.canaries, self.shadow_by_version = canaries, shadow_by_version
        self.cfg = cfg or SimpleNamespace(min_promote_accuracy=None, min_rollback_accuracy=None)
        self.updates, self.logs, self.commits, self.rollbacks = [], [], 0, 0
        self._status_row = SimpleNamespace(status="canary", eval_report={"ok": True})

    async def execute(self, q, params=None):
        s = str(q)
        if "FROM model_canary_state" in s and "SELECT" in s:
            return self.canaries
        if "FROM model_shadow_predictions" in s:
            return self.shadow_by_version.get(params["version"], [])
        if "UPDATE model_canary_state" in s:
            self.updates.append(params)
            return SimpleNamespace(rowcount=1)
        if "INSERT INTO model_canary_state" in s:
            self.updates.append(params)
            return SimpleNamespace()
        if "INSERT INTO model_promotion_log" in s:
            self.logs.append(params)
            return SimpleNamespace()
        if "status, eval_report FROM language_models" in s:
            return SimpleNamespace(first=lambda: self._status_row)
        if s.startswith("UPDATE language_models") or "language_models" in s:
            return SimpleNamespace()
        raise AssertionError("unexpected query: " + s[:80])

    async def commit(self): self.commits += 1
    async def rollback(self): self.rollbacks += 1


def run(canaries, shadow, cfg=None, dry_run=False):
    db = FakeDB(canaries, shadow, cfg)
    with mock.patch.object(mc.TenantCalibrationConfig, "load", mock.AsyncMock(return_value=db.cfg)):
        result = asyncio.run(mc.model_canary_service.run_for_tenant(db, 1, dry_run=dry_run))
    return result, db


class TestRamp(unittest.TestCase):
    def test_insufficient_samples_holds(self):
        r, db = run([row()], {"v1": shadow_rows(5, 0.99)})
        self.assertEqual(r.outcomes[0].action, mc.HOLD_SAMPLES)
        self.assertFalse(r.outcomes[0].applied)
        self.assertEqual(db.commits, 0)

    def test_good_evidence_advances(self):
        r, db = run([row()], {"v1": shadow_rows(30, 0.97)})
        self.assertEqual(r.outcomes[0].action, mc.ADVANCE)
        self.assertEqual(r.outcomes[0].to_pct, 20.0)
        self.assertTrue(r.outcomes[0].applied)
        self.assertEqual(db.commits, 1)

    def test_dry_run_never_writes(self):
        r, db = run([row()], {"v1": shadow_rows(30, 0.97)}, dry_run=True)
        self.assertFalse(r.outcomes[0].applied)
        self.assertEqual(db.commits, 0)

    def test_bad_evidence_aborts_and_rejects_model(self):
        r, db = run([row()], {"v1": shadow_rows(30, 0.5)})
        self.assertEqual(r.outcomes[0].action, mc.ABORT)
        self.assertTrue(r.outcomes[0].applied)
        self.assertGreaterEqual(db.commits, 1)  # registry commit + log commit

    def test_dwell_time_holds_even_with_good_evidence(self):
        r, _ = run([row(updated_at=datetime.utcnow())], {"v1": shadow_rows(30, 0.97)})
        self.assertEqual(r.outcomes[0].action, mc.HOLD_DWELL)

    def test_final_stage_activates(self):
        r, db = run([row(pct=100.0)], {"v1": shadow_rows(30, 0.97)})
        self.assertEqual(r.outcomes[0].action, "activated")
        self.assertTrue(r.outcomes[0].applied)

    def test_bad_language_slice_blocks_advance_even_if_overall_good(self):
        # overall aggregate must still clear decide_ramp's own bar for this
        # to be a language-slice-specific block, not a plain hold_between.
        good = shadow_rows(970, 0.99) + shadow_rows(40, 0.3, lang="hi")
        r, db = run([row()], {"v1": good})
        self.assertEqual(r.outcomes[0].action, "hold_language_slice")
        self.assertEqual(db.commits, 0)

    def test_tiny_bad_language_slice_does_not_block(self):
        rows_ = shadow_rows(970, 0.99) + shadow_rows(2, 0.0, lang="xx")
        r, _ = run([row()], {"v1": rows_})
        self.assertEqual(r.outcomes[0].action, mc.ADVANCE)

    def test_uses_shared_decide_ramp_not_a_copy(self):
        # same policy object imported, not reimplemented
        self.assertIs(mc.decide_ramp, __import__("app.language.canary_ramp_service", fromlist=["decide_ramp"]).decide_ramp)

    def test_listing_failure_is_one_error_outcome(self):
        class BrokenDB(FakeDB):
            async def execute(self, q, params=None):
                if "FROM model_canary_state" in str(q):
                    raise RuntimeError("db down")
                return await super().execute(q, params)
        db = BrokenDB([row()], {})
        with mock.patch.object(mc.TenantCalibrationConfig, "load", mock.AsyncMock(return_value=db.cfg)):
            r = asyncio.run(mc.model_canary_service.run_for_tenant(db, 1))
        self.assertEqual(r.errors, 1)

    def test_one_canary_error_does_not_block_another(self):
        class PartialDB(FakeDB):
            async def execute(self, q, params=None):
                if "FROM model_shadow_predictions" in str(q) and params["version"] == "bad":
                    raise RuntimeError("boom")
                return await super().execute(q, params)
        db = PartialDB([row("bad"), row("v1")], {"v1": shadow_rows(30, 0.97)})
        with mock.patch.object(mc.TenantCalibrationConfig, "load", mock.AsyncMock(return_value=db.cfg)):
            r = asyncio.run(mc.model_canary_service.run_for_tenant(db, 1))
        self.assertEqual(r.errors, 1)
        self.assertEqual(len([o for o in r.outcomes if o.action == mc.ADVANCE]), 1)


class TestStartCanary(unittest.TestCase):
    def test_start_canary_requires_legal_transition(self):
        db = FakeDB([], {})
        db._status_row = SimpleNamespace(status="trained", eval_report={"ok": True})
        with self.assertRaises(mr.RegistryError):
            asyncio.run(mc.start_canary(db, tenant_id=1, version="v1"))

    def test_start_canary_ok_from_shadow(self):
        db = FakeDB([], {})
        db._status_row = SimpleNamespace(status="shadow", eval_report={"ok": True})
        asyncio.run(mc.start_canary(db, tenant_id=1, version="v1"))
        self.assertGreaterEqual(db.commits, 1)


class TestWiring(unittest.TestCase):
    def test_default_off_nothing_serves(self):
        from pathlib import Path
        root = Path(mc.__file__).parents[1]
        for f in ("kernel/kernel.py", "language/routing_service.py", "agent/core_agent.py"):
            self.assertNotIn("model_canary_service", (root / f).read_text(encoding="utf-8"))
            
if __name__ == "__main__":
    unittest.main()
