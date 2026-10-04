"""
Tests for app/language/calibration_scheduler.py.

Fake sessions dispatch on SQL text; CalibrationService is patched. No
Postgres. NOT exercised: the SQL against a real database and the real
CalibrationService behaviour under the scheduler (that service has its
own tests) -- run the CLI once with --dry-run in Docker.

Run:
    docker compose exec python-api python -m unittest tests.test_calibration_scheduler -v
"""

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.language import calibration_scheduler as cs
from app.language.calibration_types import CalibrationResult, IntentCalibrationStats


def run(coro):
    return asyncio.run(coro)


class Res:
    def __init__(self, rows=None):
        self._rows = rows or []

    def first(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class FakeDb:
    def __init__(self, active_tenants, shadow_counts):
        self.active_tenants = active_tenants
        self.shadow_counts = shadow_counts
        self.executed = []

    async def execute(self, q, p=None):
        sql = str(q)
        self.executed.append((sql, p))
        if "FROM tenants" in sql:
            wanted = (p or {}).get("tenant_id")
            return Res([SimpleNamespace(id=t) for t in self.active_tenants
                        if wanted is None or wanted == t])
        if "FROM language_experiences" in sql:
            return Res([SimpleNamespace(n=self.shadow_counts.get(p["tenant_id"], 0))])
        raise AssertionError(f"unexpected SQL: {sql[:60]}")


class Factory:
    def __init__(self, active_tenants=(), shadow_counts=None):
        self.active_tenants = list(active_tenants)
        self.shadow_counts = shadow_counts or {}
        self.dbs = []

    def __call__(self):
        f = self

        class CM:
            async def __aenter__(cm):
                db = FakeDb(f.active_tenants, f.shadow_counts)
                f.dbs.append(db)
                return db

            async def __aexit__(cm, *a):
                return False
        return CM()

    @property
    def opened(self):
        return len(self.dbs)


def fake_lock(acquired):
    @asynccontextmanager
    async def lock(name):
        lock.name = name
        yield acquired
    return lock


def cal_result(tid, samples=100, updated=2):
    return CalibrationResult(
        tenant_id=tid, intents_updated=updated, total_samples=samples,
        per_intent=[
            IntentCalibrationStats("PRICE_INQUIRY", 0.75, 0.93, 60),
            IntentCalibrationStats("COMPLAINT", None, None, 40),
        ],
    )


def patched_service(side_effect=None):
    return patch.object(
        cs.calibration_service, "run_for_tenant",
        new=AsyncMock(side_effect=side_effect or (lambda db, tid: cal_result(tid))),
    )


class TestResultToDict(unittest.TestCase):
    def test_shape_and_null_thresholds(self):
        d = cs.result_to_dict(cal_result(3))
        self.assertEqual((d["tenantId"], d["totalSamples"], d["intentsUpdated"]), (3, 100, 2))
        self.assertEqual(d["perIntent"][0], {
            "intent": "PRICE_INQUIRY", "sampleCount": 60,
            "similarityThreshold": 0.75, "agreementRate": 0.93})
        self.assertIsNone(d["perIntent"][1]["similarityThreshold"])
        json.dumps(d)


class TestRunAll(unittest.TestCase):
    def test_lock_not_acquired_skips_everything(self):
        f = Factory([1, 2])
        with patched_service() as svc:
            out = run(cs.run_all(session_factory=f, lock=fake_lock(False)))
        self.assertTrue(out["skipped"])
        self.assertEqual(f.opened, 0)
        svc.assert_not_called()

    def test_uses_named_lock_distinct_from_ramp(self):
        from app.language import canary_ramp_service
        lock = fake_lock(True)
        run(cs.run_all(session_factory=Factory([]), lock=lock))
        self.assertEqual(lock.name, "calibration")
        self.assertNotEqual(cs.LOCK_NAME, canary_ramp_service.LOCK_NAME)

    def test_calibrates_every_active_tenant_with_fresh_sessions(self):
        f = Factory([1, 2, 5])
        with patched_service() as svc:
            out = run(cs.run_all(session_factory=f, lock=fake_lock(True)))
        self.assertEqual([c.args[1] for c in svc.await_args_list], [1, 2, 5])
        self.assertEqual([t["tenantId"] for t in out["tenants"]], [1, 2, 5])
        self.assertEqual(out["errors"], 0)
        self.assertFalse(out["dryRun"])
        self.assertEqual(f.opened, 1 + 3)  # listing + one per tenant
        sessions_used = {id(c.args[0]) for c in svc.await_args_list}
        self.assertEqual(len(sessions_used), 3)

    def test_only_active_tenants_are_listed(self):
        f = Factory([])
        run(cs.run_all(session_factory=f, lock=fake_lock(True)))
        self.assertIn("is_active = TRUE", f.dbs[0].executed[0][0])

    def test_tenant_filter(self):
        f = Factory([1, 2, 3])
        with patched_service() as svc:
            out = run(cs.run_all(tenant_id=2, session_factory=f, lock=fake_lock(True)))
        self.assertEqual([t["tenantId"] for t in out["tenants"]], [2])
        self.assertEqual(svc.await_count, 1)
        sql, params = f.dbs[0].executed[0]
        self.assertIn("id = :tenant_id", sql)
        self.assertEqual(params, {"tenant_id": 2})

    def test_unknown_or_inactive_tenant_is_an_error(self):
        f = Factory([1])
        with patched_service() as svc:
            out = run(cs.run_all(tenant_id=99, session_factory=f, lock=fake_lock(True)))
        self.assertEqual(out["errors"], 1)
        self.assertIn("99", out["error"])
        svc.assert_not_called()

    def test_no_active_tenants_is_a_clean_empty_run(self):
        with patched_service() as svc:
            out = run(cs.run_all(session_factory=Factory([]), lock=fake_lock(True)))
        self.assertEqual((out["errors"], out["tenants"]), (0, []))
        svc.assert_not_called()

    def test_one_tenant_raising_does_not_block_the_rest(self):
        def side(db, tid):
            if tid == 2:
                raise RuntimeError("boom")
            return cal_result(tid)

        with patched_service(side) as svc:
            out = run(cs.run_all(session_factory=Factory([1, 2, 3]), lock=fake_lock(True)))
        self.assertEqual(svc.await_count, 3)
        self.assertEqual(out["errors"], 1)
        self.assertIn("error", out["tenants"][1])
        self.assertEqual(out["tenants"][2]["tenantId"], 3)

    def test_dry_run_never_calls_the_service_and_reports_row_counts(self):
        f = Factory([1, 2], shadow_counts={1: 250, 2: 0})
        with patched_service() as svc:
            out = run(cs.run_all(dry_run=True, session_factory=f, lock=fake_lock(True)))
        svc.assert_not_called()
        self.assertTrue(out["dryRun"])
        self.assertEqual(out["tenants"], [
            {"tenantId": 1, "wouldCalibrate": True, "shadowRows": 250},
            {"tenantId": 2, "wouldCalibrate": False, "shadowRows": 0},
        ])

    def test_dry_run_only_reads(self):
        f = Factory([1], shadow_counts={1: 5})
        run(cs.run_all(dry_run=True, session_factory=f, lock=fake_lock(True)))
        for db in f.dbs:
            for sql, _ in db.executed:
                self.assertTrue(sql.lstrip().upper().startswith("SELECT"), sql[:40])

    def test_dry_run_count_query_is_tenant_scoped(self):
        f = Factory([7], shadow_counts={7: 1})
        run(cs.run_all(dry_run=True, session_factory=f, lock=fake_lock(True)))
        sql, params = f.dbs[1].executed[0]
        self.assertIn("tenant_id = :tenant_id", sql)
        self.assertEqual(params, {"tenant_id": 7})

    def test_summary_json_serialisable(self):
        with patched_service():
            out = run(cs.run_all(session_factory=Factory([1, 2]), lock=fake_lock(True)))
        json.dumps(out)


class TestMain(unittest.TestCase):
    def test_exit_0_on_clean_run_and_passes_args(self):
        summary = {"skipped": False, "dryRun": True, "errors": 0, "tenants": []}
        with patch.object(cs, "run_all", new=AsyncMock(return_value=summary)) as m, \
             patch("builtins.print"):
            self.assertEqual(cs.main(["--tenant", "4", "--dry-run"]), 0)
        self.assertEqual(m.await_args.kwargs, {"tenant_id": 4, "dry_run": True})

    def test_exit_1_when_errors(self):
        summary = {"skipped": False, "dryRun": False, "errors": 2, "tenants": []}
        with patch.object(cs, "run_all", new=AsyncMock(return_value=summary)), \
             patch("builtins.print"):
            self.assertEqual(cs.main([]), 1)

    def test_exit_0_when_skipped_by_lock(self):
        summary = {"skipped": True, "dryRun": False, "errors": 0, "tenants": []}
        with patch.object(cs, "run_all", new=AsyncMock(return_value=summary)), \
             patch("builtins.print"):
            self.assertEqual(cs.main([]), 0)

    def test_defaults_are_daily(self):
        self.assertEqual(cs.DEFAULT_INTERVAL_HOURS, 24.0)


if __name__ == "__main__":
    unittest.main()
