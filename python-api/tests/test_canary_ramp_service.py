"""
Tests for app/language/canary_ramp_service.py, scheduler_lock.py and
PromotionService.abort_canary().

Fake sessions dispatch on SQL text; no Postgres. NOT exercised: the SQL
itself against a real database (interval math, UPDATE row counts, the
advisory lock) -- run the CLI once with --dry-run in Docker.

Run:
    docker compose exec python-api python -m unittest tests.test_canary_ramp_service -v
"""

import asyncio
import unittest
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.language import canary_ramp_service as crs
from app.language import scheduler_lock as sl
from app.language.cluster_builder_types import PromotionCheckResult
from app.language.promotion_service import PromotionService
from app.language.tenant_calibration_config import TenantCalibrationConfig

T0 = datetime(2026, 9, 28, 0, 0, 0)


def run(coro):
    return asyncio.run(coro)


# ── fakes ────────────────────────────────────────────────────────────

class Res:
    def __init__(self, rows=None, rowcount=1, scalar=None):
        self._rows = rows or []
        self.rowcount = rowcount
        self._scalar = scalar

    def first(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)

    def scalar(self):
        return self._scalar


class Dispatcher:
    """handlers: list of (sql_substring, callable(params)->Res | Res | Exception)."""

    def __init__(self, handlers):
        self.handlers = handlers
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, q, p=None):
        sql = str(q)
        self.executed.append((sql, p))
        for needle, h in self.handlers:
            if needle in sql:
                out = h(p) if callable(h) else h
                if isinstance(out, Exception):
                    raise out
                return out
        raise AssertionError(f"unexpected SQL: {sql[:80]}")

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    def sql_containing(self, needle):
        return [(s, p) for s, p in self.executed if needle in s]


def canary(intent="PRICE_INQUIRY", pct=5.0, age=30.0, calibrated=True, cluster=77):
    return SimpleNamespace(
        intent=intent, candidate_cluster_id=cluster, canary_pct=pct, updated_at=T0,
        stage_age_hours=age, candidate_calibrated=calibrated,
    )


def stats(n, rate):
    return SimpleNamespace(n=n, rate=rate)


def make_db(canaries, stats_row=None, advance_rowcount=1):
    return Dispatcher([
        ("JOIN intent_clusters ic", Res(canaries)),
        ("FROM routing_decisions", Res([stats_row] if stats_row is not None else [])),
        ("UPDATE canary_splits", Res(rowcount=advance_rowcount)),
        ("INSERT INTO cluster_promotion_log", Res()),
    ])


CFG = TenantCalibrationConfig(tenant_id=1)  # promote 0.95, rollback 0.85


def run_tenant(db, cfg=CFG, **kw):
    with patch.object(TenantCalibrationConfig, "load", new=AsyncMock(return_value=cfg)):
        return run(crs.canary_ramp_service.run_for_tenant(db, 1, **kw))


# ── pure logic ───────────────────────────────────────────────────────

class TestLadder(unittest.TestCase):
    def test_ladder_matches_design_doc(self):
        self.assertEqual(crs.RAMP_STAGES, (5.0, 20.0, 50.0, 100.0))

    def test_next_stage(self):
        self.assertEqual(crs.next_stage(5.0), 20.0)
        self.assertEqual(crs.next_stage(20.0), 50.0)
        self.assertEqual(crs.next_stage(50.0), 100.0)
        self.assertIsNone(crs.next_stage(100.0))

    def test_next_stage_from_off_ladder_starting_pct(self):
        self.assertEqual(crs.next_stage(1.0), 5.0)
        self.assertEqual(crs.next_stage(10.0), 20.0)
        self.assertEqual(crs.next_stage(35.0), 50.0)
        self.assertEqual(crs.next_stage(99.9), 100.0)

    def test_is_final(self):
        self.assertTrue(crs.is_final(100.0))
        self.assertFalse(crs.is_final(50.0))
        self.assertFalse(crs.is_final(99.0))

    def test_sample_floor_tracks_promotion_service(self):
        from app.language.promotion_service import MIN_CANARY_SAMPLES
        self.assertEqual(crs.MIN_STAGE_SAMPLES, MIN_CANARY_SAMPLES)


def decide(**over):
    base = dict(pct=5.0, stage_samples=50, stage_rate=0.99, stage_age_hours=30.0,
                min_promote_accuracy=0.95, min_rollback_accuracy=0.85)
    base.update(over)
    return crs.decide_ramp(**base)


class TestDecideRamp(unittest.TestCase):
    def test_good_stage_advances(self):
        d = decide()
        self.assertEqual((d.action, d.next_pct), (crs.ADVANCE, 20.0))

    def test_each_stage_advances_to_the_next(self):
        for pct, nxt in ((5.0, 20.0), (20.0, 50.0), (50.0, 100.0)):
            with self.subTest(pct):
                d = decide(pct=pct)
                self.assertEqual((d.action, d.next_pct), (crs.ADVANCE, nxt))

    def test_final_stage_hands_to_promotion_not_advance(self):
        d = decide(pct=100.0)
        self.assertEqual(d.action, crs.PROMOTE_CHECK)
        self.assertIsNone(d.next_pct)

    def test_too_few_samples_holds_even_if_perfect(self):
        d = decide(stage_samples=19, stage_rate=1.0)
        self.assertEqual(d.action, crs.HOLD_SAMPLES)

    def test_too_few_samples_holds_even_if_terrible(self):
        # 3 wrong out of 3 is not evidence yet
        self.assertEqual(decide(stage_samples=3, stage_rate=0.0).action, crs.HOLD_SAMPLES)

    def test_no_rate_holds(self):
        self.assertEqual(decide(stage_samples=50, stage_rate=None).action, crs.HOLD_SAMPLES)

    def test_exactly_min_samples_is_enough(self):
        self.assertEqual(decide(stage_samples=20).action, crs.ADVANCE)

    def test_regression_aborts(self):
        d = decide(stage_rate=0.80)
        self.assertEqual(d.action, crs.ABORT)

    def test_regression_aborts_without_waiting_for_dwell(self):
        self.assertEqual(decide(stage_rate=0.80, stage_age_hours=0.5).action, crs.ABORT)

    def test_good_but_too_young_holds_for_dwell(self):
        self.assertEqual(decide(stage_age_hours=2.0).action, crs.HOLD_DWELL)

    def test_dwell_boundary_inclusive(self):
        self.assertEqual(decide(stage_age_hours=24.0).action, crs.ADVANCE)

    def test_between_thresholds_holds(self):
        self.assertEqual(decide(stage_rate=0.90).action, crs.HOLD_BETWEEN)

    def test_boundaries(self):
        self.assertEqual(decide(stage_rate=0.95).action, crs.ADVANCE)         # >= promote
        self.assertEqual(decide(stage_rate=0.85).action, crs.HOLD_BETWEEN)    # rollback is strict <
        self.assertEqual(decide(stage_rate=0.8499).action, crs.ABORT)

    def test_tenant_thresholds_respected(self):
        d = decide(stage_rate=0.92, min_promote_accuracy=0.90, min_rollback_accuracy=0.80)
        self.assertEqual(d.action, crs.ADVANCE)
        d = decide(stage_rate=0.92, min_promote_accuracy=0.99, min_rollback_accuracy=0.95)
        self.assertEqual(d.action, crs.ABORT)


# ── run_for_tenant ───────────────────────────────────────────────────

class TestRunForTenant(unittest.TestCase):
    def setUp(self):
        self.promo_check = patch.object(
            crs.promotion_service, "check_and_promote",
            new=AsyncMock(return_value=PromotionCheckResult(intent="PRICE_INQUIRY", action="promoted")),
        )
        self.promo_abort = patch.object(
            crs.promotion_service, "abort_canary",
            new=AsyncMock(return_value=PromotionCheckResult(intent="PRICE_INQUIRY", action="rolled_back")),
        )
        self.check = self.promo_check.start()
        self.abort = self.promo_abort.start()
        self.addCleanup(self.promo_check.stop)
        self.addCleanup(self.promo_abort.stop)

    def test_advance_5_to_20_writes_update_and_audit_row(self):
        db = make_db([canary(pct=5.0)], stats(60, 0.98))
        res = run_tenant(db)
        o = res.outcomes[0]
        self.assertEqual((o.action, o.from_pct, o.to_pct, o.applied), (crs.ADVANCE, 5.0, 20.0, True))
        upd = db.sql_containing("UPDATE canary_splits")
        self.assertEqual(len(upd), 1)
        sql, params = upd[0]
        self.assertNotIn("started_at", sql)              # mid-ladder step keeps the window
        self.assertEqual(params["next_pct"], 20.0)
        self.assertEqual(params["old_pct"], 5.0)         # optimistic guard
        self.assertEqual(params["cluster_id"], 77)
        self.assertEqual(params["tenant_id"], 1)
        log = db.sql_containing("INSERT INTO cluster_promotion_log")
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0][1]["canary_pct"], 20.0)
        self.assertIn("5->20", log[0][1]["notes"])
        self.assertEqual(db.commits, 2)                  # ramp, then audit row
        self.check.assert_not_called()                   # never promotes mid-ladder
        self.abort.assert_not_called()

    def test_advance_to_100_restarts_evidence_window(self):
        db = make_db([canary(pct=50.0)], stats(60, 0.98))
        run_tenant(db)
        sql, params = db.sql_containing("UPDATE canary_splits")[0]
        self.assertIn("started_at = NOW()", sql)
        self.assertEqual(params["next_pct"], 100.0)

    def test_stage_stats_are_scoped_to_tenant_intent_and_stage_start(self):
        db = make_db([canary(intent="COMPLAINT", pct=20.0)], stats(60, 0.98))
        run_tenant(db)
        sql, params = db.sql_containing("FROM routing_decisions")[0]
        # C8: evidence is also scoped to the candidate cluster (canary() default id).
        self.assertEqual(
            params,
            {"tenant_id": 1, "intent": "COMPLAINT", "since": T0,
             "cluster_id": params["cluster_id"]},
        )
        self.assertIsNotNone(params["cluster_id"])
        self.assertIn("routed_to        = 'brain'", sql)
        self.assertIn("created_at       >= :since", sql)
        # C8: only turns the candidate cluster actually served count.
        self.assertIn("served_branch    = 'candidate'", sql)
        self.assertIn("served_cluster_id = :cluster_id", sql)

    def test_insufficient_samples_holds_and_writes_nothing(self):
        db = make_db([canary()], stats(5, 1.0))
        res = run_tenant(db)
        self.assertEqual(res.outcomes[0].action, crs.HOLD_SAMPLES)
        self.assertFalse(res.outcomes[0].applied)
        self.assertEqual(db.sql_containing("UPDATE"), [])
        self.assertEqual(db.commits, 0)

    def test_no_stats_row_is_zero_samples(self):
        db = make_db([canary()], None)
        self.assertEqual(run_tenant(db).outcomes[0].action, crs.HOLD_SAMPLES)

    def test_young_stage_holds_for_dwell(self):
        db = make_db([canary(age=3.0)], stats(60, 0.99))
        self.assertEqual(run_tenant(db).outcomes[0].action, crs.HOLD_DWELL)
        self.assertEqual(db.sql_containing("UPDATE"), [])

    def test_min_dwell_hours_override(self):
        db = make_db([canary(age=3.0)], stats(60, 0.99))
        self.assertEqual(run_tenant(db, min_dwell_hours=0).outcomes[0].action, crs.ADVANCE)

    def test_between_thresholds_holds(self):
        db = make_db([canary()], stats(60, 0.90))
        self.assertEqual(run_tenant(db).outcomes[0].action, crs.HOLD_BETWEEN)

    def test_regression_aborts_via_promotion_service(self):
        db = make_db([canary(pct=20.0)], stats(60, 0.70))
        o = run_tenant(db).outcomes[0]
        self.assertEqual((o.action, o.applied, o.promotion_action), (crs.ABORT, True, "rolled_back"))
        self.abort.assert_awaited_once()
        kw = self.abort.await_args.kwargs
        self.assertEqual(kw["sample_count"], 60)
        self.assertAlmostEqual(kw["was_correct_rate"], 0.70)
        self.assertIn("canary ramp", kw["notes"])
        self.assertEqual(db.sql_containing("UPDATE canary_splits"), [])  # no advance
        self.check.assert_not_called()

    def test_final_stage_good_hands_over_to_check_and_promote(self):
        db = make_db([canary(pct=100.0)], stats(60, 0.99))
        o = run_tenant(db).outcomes[0]
        self.assertEqual((o.action, o.promotion_action, o.applied), (crs.PROMOTE_CHECK, "promoted", True))
        self.check.assert_awaited_once()
        self.assertEqual(db.sql_containing("UPDATE canary_splits"), [])

    def test_final_stage_gate_pending_is_not_applied(self):
        self.check.return_value = PromotionCheckResult(intent="X", action="pending")
        db = make_db([canary(pct=100.0)], stats(60, 0.99))
        o = run_tenant(db).outcomes[0]
        self.assertEqual(o.promotion_action, "pending")
        self.assertFalse(o.applied)

    def test_promotion_service_error_becomes_error_outcome(self):
        self.check.return_value = PromotionCheckResult(intent="X", action="error")
        db = make_db([canary(pct=100.0)], stats(60, 0.99))
        res = run_tenant(db)
        self.assertEqual(res.outcomes[0].action, crs.ERROR)
        self.assertEqual(res.errors, 1)

    def test_check_and_promote_never_called_below_100(self):
        for pct in (5.0, 20.0, 50.0):
            with self.subTest(pct):
                db = make_db([canary(pct=pct)], stats(60, 0.99))
                run_tenant(db)
        self.check.assert_not_called()

    def test_uncalibrated_candidate_reports_and_skips_stats(self):
        db = make_db([canary(calibrated=False)], stats(60, 0.99))
        o = run_tenant(db).outcomes[0]
        self.assertEqual(o.action, crs.HOLD_UNCALIBRATED)
        self.assertIn("calibrat", o.reason)
        self.assertEqual(db.sql_containing("FROM routing_decisions"), [])

    def test_dry_run_decides_but_writes_and_calls_nothing(self):
        for pct, expect in ((5.0, crs.ADVANCE), (100.0, crs.PROMOTE_CHECK)):
            with self.subTest(pct):
                db = make_db([canary(pct=pct)], stats(60, 0.99))
                o = run_tenant(db, dry_run=True).outcomes[0]
                self.assertEqual(o.action, expect)
                self.assertFalse(o.applied)
                self.assertEqual(db.sql_containing("UPDATE"), [])
                self.assertEqual(db.sql_containing("INSERT"), [])
                self.assertEqual(db.commits, 0)
        self.check.assert_not_called()
        db = make_db([canary(pct=20.0)], stats(60, 0.5))
        self.assertEqual(run_tenant(db, dry_run=True).outcomes[0].action, crs.ABORT)
        self.abort.assert_not_called()

    def test_concurrent_change_writes_nothing_and_rolls_back(self):
        db = make_db([canary()], stats(60, 0.99), advance_rowcount=0)
        o = run_tenant(db).outcomes[0]
        self.assertEqual(o.action, "skipped_changed_concurrently")
        self.assertFalse(o.applied)
        self.assertEqual(db.commits, 0)
        self.assertEqual(db.rollbacks, 1)
        self.assertEqual(db.sql_containing("INSERT"), [])

    def test_audit_failure_does_not_undo_or_fail_the_ramp(self):
        db = Dispatcher([
            ("JOIN intent_clusters ic", Res([canary()])),
            ("FROM routing_decisions", Res([stats(60, 0.99)])),
            ("UPDATE canary_splits", Res(rowcount=1)),
            ("INSERT INTO cluster_promotion_log", RuntimeError("log table down")),
        ])
        o = run_tenant(db).outcomes[0]
        self.assertEqual((o.action, o.applied), (crs.ADVANCE, True))
        self.assertEqual(db.rollbacks, 1)

    def test_one_failing_canary_does_not_block_the_others(self):
        calls = {"n": 0}

        def stats_handler(params):
            calls["n"] += 1
            if params["intent"] == "COMPLAINT":
                return RuntimeError("boom")
            return Res([stats(60, 0.99)])

        db = Dispatcher([
            ("JOIN intent_clusters ic", Res([canary(intent="COMPLAINT"), canary(intent="PRICE_INQUIRY")])),
            ("FROM routing_decisions", stats_handler),
            ("UPDATE canary_splits", Res(rowcount=1)),
            ("INSERT INTO cluster_promotion_log", Res()),
        ])
        res = run_tenant(db)
        self.assertEqual([o.action for o in res.outcomes], [crs.ERROR, crs.ADVANCE])
        self.assertEqual(res.errors, 1)
        self.assertEqual(db.rollbacks, 1)

    def test_listing_failure_is_reported_not_raised(self):
        db = Dispatcher([("JOIN intent_clusters ic", RuntimeError("db down"))])
        res = run_tenant(db)
        self.assertEqual(res.errors, 1)
        self.assertEqual(res.outcomes[0].intent, "*")

    def test_no_canaries_no_outcomes(self):
        self.assertEqual(run_tenant(make_db([])).outcomes, [])

    def test_tenant_config_thresholds_drive_decisions(self):
        lenient = TenantCalibrationConfig(tenant_id=1, min_promote_accuracy=0.90, min_rollback_accuracy=0.80)
        db = make_db([canary()], stats(60, 0.92))
        self.assertEqual(run_tenant(db, cfg=lenient).outcomes[0].action, crs.ADVANCE)
        db = make_db([canary()], stats(60, 0.92))
        self.assertEqual(run_tenant(db, cfg=CFG).outcomes[0].action, crs.HOLD_BETWEEN)

    def test_outcome_is_json_serialisable(self):
        import json
        db = make_db([canary()], stats(60, 0.98))
        json.dumps(run_tenant(db).to_dict())

    def test_list_query_is_tenant_scoped(self):
        db = make_db([])
        run_tenant(db)
        sql, params = db.sql_containing("JOIN intent_clusters ic")[0]
        self.assertIn("cs.tenant_id = :tenant_id", sql)
        self.assertEqual(params, {"tenant_id": 1})


# ── run_all (all tenants + lock) ─────────────────────────────────────

def fake_lock(acquired):
    @asynccontextmanager
    async def lock(name):
        lock.name = name
        yield acquired
    return lock


class FakeSessionFactory:
    def __init__(self, tenant_rows):
        self.tenant_rows = tenant_rows
        self.opened = 0
        self.sessions = []

    def __call__(self):
        factory = self

        class CM:
            async def __aenter__(cm):
                factory.opened += 1
                db = Dispatcher([
                    ("SELECT DISTINCT cs.tenant_id", lambda p: Res(
                        [SimpleNamespace(tenant_id=t) for t in factory.tenant_rows
                         if p is None or p.get("tenant_id") in (None, t)])),
                ])
                factory.sessions.append(db)
                return db

            async def __aexit__(cm, *a):
                return False
        return CM()


class TestRunAll(unittest.TestCase):
    def test_lock_not_acquired_skips_without_touching_db(self):
        sf = FakeSessionFactory([1, 2])
        out = run(crs.run_all(session_factory=sf, lock=fake_lock(False)))
        self.assertTrue(out["skipped"])
        self.assertEqual(sf.opened, 0)

    def test_uses_named_lock(self):
        lock = fake_lock(True)
        run(crs.run_all(session_factory=FakeSessionFactory([]), lock=lock))
        self.assertEqual(lock.name, crs.LOCK_NAME)

    def test_runs_each_tenant_in_its_own_session_and_aggregates_errors(self):
        sf = FakeSessionFactory([1, 2])

        async def fake_run(db, tid, **kw):
            outcomes = [crs.RampOutcome(intent="A", action=crs.ERROR if tid == 2 else crs.HOLD_SAMPLES)]
            return crs.RampRunResult(tid, outcomes)

        with patch.object(crs.canary_ramp_service, "run_for_tenant", new=AsyncMock(side_effect=fake_run)) as m:
            out = run(crs.run_all(session_factory=sf, lock=fake_lock(True), dry_run=True, min_dwell_hours=1))
        self.assertFalse(out["skipped"])
        self.assertTrue(out["dryRun"])
        self.assertEqual(out["errors"], 1)
        self.assertEqual([t["tenantId"] for t in out["tenants"]], [1, 2])
        self.assertEqual(sf.opened, 1 + 2)  # tenant listing + one per tenant
        self.assertEqual(m.await_count, 2)
        self.assertTrue(m.await_args.kwargs["dry_run"])
        self.assertEqual(m.await_args.kwargs["min_dwell_hours"], 1)

    def test_tenant_filter_uses_tenant_query(self):
        sf = FakeSessionFactory([1, 2])
        with patch.object(crs.canary_ramp_service, "run_for_tenant",
                          new=AsyncMock(side_effect=lambda db, tid, **k: crs.RampRunResult(tid, []))):
            out = run(crs.run_all(tenant_id=2, session_factory=sf, lock=fake_lock(True)))
        self.assertEqual([t["tenantId"] for t in out["tenants"]], [2])
        listing_sql, params = sf.sessions[0].executed[0]
        self.assertIn("cs.tenant_id = :tenant_id", listing_sql)
        self.assertEqual(params, {"tenant_id": 2})

    def test_tenant_listing_only_active_tenants(self):
        sf = FakeSessionFactory([])
        run(crs.run_all(session_factory=sf, lock=fake_lock(True)))
        self.assertIn("t.is_active = TRUE", sf.sessions[0].executed[0][0])

    def test_json_summary_serialisable(self):
        import json
        sf = FakeSessionFactory([1])
        with patch.object(crs.canary_ramp_service, "run_for_tenant",
                          new=AsyncMock(side_effect=lambda db, tid, **k: crs.RampRunResult(
                              tid, [crs.RampOutcome(intent="A", action=crs.ADVANCE, to_pct=20.0)]))):
            json.dumps(run(crs.run_all(session_factory=sf, lock=fake_lock(True))), default=str)


# ── PromotionService.abort_canary ────────────────────────────────────

class TestAbortCanary(unittest.TestCase):
    def db(self, canary_rows):
        return Dispatcher([
            ("FROM canary_splits", Res(canary_rows)),
            ("UPDATE intent_clusters", Res()),
            ("DELETE FROM canary_splits", Res()),
            ("INSERT INTO cluster_promotion_log", Res()),
        ])

    def test_retires_candidate_clears_canary_and_logs_canary_fail(self):
        db = self.db([SimpleNamespace(candidate_cluster_id=77, canary_pct=20.0)])
        res = run(PromotionService().abort_canary(
            db, 1, "PRICE_INQUIRY", was_correct_rate=0.7, sample_count=60, notes="why"))
        self.assertEqual((res.action, res.cluster_id), ("rolled_back", 77))
        self.assertEqual(len(db.sql_containing("UPDATE intent_clusters")), 1)
        self.assertEqual(len(db.sql_containing("DELETE FROM canary_splits")), 1)
        log = db.sql_containing("INSERT INTO cluster_promotion_log")[0][1]
        self.assertEqual(log["event_type"], "canary_fail")
        self.assertEqual(log["notes"], "why")
        self.assertEqual(log["canary_pct"], 20.0)
        self.assertEqual(log["sample_count"], 60)

    def test_never_promotes(self):
        db = self.db([SimpleNamespace(candidate_cluster_id=77, canary_pct=20.0)])
        run(PromotionService().abort_canary(
            db, 1, "X", was_correct_rate=0.1, sample_count=30, notes="n"))
        for sql, _ in db.executed:
            self.assertNotIn("is_promoted  = TRUE", sql)

    def test_no_canary(self):
        db = self.db([])
        res = run(PromotionService().abort_canary(
            db, 1, "X", was_correct_rate=None, sample_count=None, notes="n"))
        self.assertEqual(res.action, "no_canary")
        self.assertEqual(db.sql_containing("UPDATE"), [])

    def test_never_raises(self):
        db = Dispatcher([("FROM canary_splits", RuntimeError("db down"))])
        res = run(PromotionService().abort_canary(
            db, 1, "X", was_correct_rate=None, sample_count=None, notes="n"))
        self.assertEqual(res.action, "error")


# ── scheduler_lock ───────────────────────────────────────────────────

class FakeConn:
    def __init__(self, acquired=True, fail_unlock=False):
        self.acquired = acquired
        self.fail_unlock = fail_unlock
        self.executed = []

    async def execute(self, q, p=None):
        self.executed.append((str(q), p))
        if "pg_try_advisory_lock" in str(q):
            return Res(scalar=self.acquired)
        if self.fail_unlock:
            raise RuntimeError("conn closed")
        return Res()


def connect_for(conn):
    @asynccontextmanager
    async def connect():
        yield conn
    return connect


class TestSchedulerLock(unittest.TestCase):
    def test_key_is_stable_signed_64bit_and_name_specific(self):
        a = sl.lock_key("canary_ramp")
        self.assertEqual(a, sl.lock_key("canary_ramp"))
        self.assertNotEqual(a, sl.lock_key("calibration"))
        self.assertTrue(-(2 ** 63) <= a < 2 ** 63)

    def test_acquired_then_unlocked(self):
        conn = FakeConn(True)

        async def go():
            async with sl.advisory_job_lock("j", connect=connect_for(conn)) as ok:
                return ok
        self.assertTrue(run(go()))
        self.assertEqual(len(conn.executed), 2)
        self.assertIn("pg_advisory_unlock", conn.executed[1][0])
        self.assertEqual(conn.executed[0][1], conn.executed[1][1])

    def test_not_acquired_does_not_unlock(self):
        conn = FakeConn(False)

        async def go():
            async with sl.advisory_job_lock("j", connect=connect_for(conn)) as ok:
                return ok
        self.assertFalse(run(go()))
        self.assertEqual(len(conn.executed), 1)

    def test_unlocked_even_if_body_raises(self):
        conn = FakeConn(True)

        async def go():
            async with sl.advisory_job_lock("j", connect=connect_for(conn)):
                raise ValueError("job blew up")
        with self.assertRaises(ValueError):
            run(go())
        self.assertIn("pg_advisory_unlock", conn.executed[-1][0])

    def test_unlock_failure_does_not_mask_result(self):
        conn = FakeConn(True, fail_unlock=True)

        async def go():
            async with sl.advisory_job_lock("j", connect=connect_for(conn)) as ok:
                return ok
        self.assertTrue(run(go()))


if __name__ == "__main__":
    unittest.main()
