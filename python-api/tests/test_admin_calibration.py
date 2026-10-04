"""
Tests for app/api/routes/admin_calibration.py.

Pure validation + handlers driven through a scripted fake session. No
Postgres and (for the pure part) no FastAPI needed. NOT exercised:
real FastAPI request parsing / pydantic `extra="forbid"` behaviour and
real SQL against Postgres -- hit the routes once in Docker.

Run:
    docker compose exec python-api python -m unittest tests.test_admin_calibration -v
"""

import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace

from fastapi import HTTPException

from app.api.routes import admin_calibration as ac
from app.language.tenant_calibration_config import TenantCalibrationConfig


def run(coro):
    return asyncio.run(coro)


DEFAULTS = ac.config_defaults()
NOW = datetime(2026, 9, 28, 12, 0, 0)


def stored_row(**over):
    base = dict(
        min_agreement_rate=0.90, min_sample_count=30,
        min_promote_accuracy=0.95, min_rollback_accuracy=0.85,
        initial_canary_pct=5.0, novelty_threshold=0.50,
        novelty_logging_enabled=True, updated_at=NOW,
    )
    base.update(over)
    return SimpleNamespace(**base)


class FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class ScriptedSession:
    """Returns queued rows in order for SELECTs; records everything."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []   # (sql, params)
        self.commits = 0

    async def execute(self, q, p=None):
        self.executed.append((str(q), p))
        if str(q).lstrip().upper().startswith("SELECT"):
            return FakeResult(self.rows.pop(0))
        return FakeResult(None)

    async def commit(self):
        self.commits += 1


class TestDefaults(unittest.TestCase):
    def test_defaults_come_from_dataclass_and_cover_all_fields(self):
        self.assertEqual(set(DEFAULTS), set(ac.CONFIG_FIELDS))
        self.assertEqual(DEFAULTS["min_agreement_rate"], 0.90)
        self.assertEqual(DEFAULTS["initial_canary_pct"], 5.0)
        self.assertIs(DEFAULTS["novelty_logging_enabled"], True)

    def test_camel_snake_maps_are_consistent(self):
        self.assertEqual(set(ac._CAMEL_TO_SNAKE.values()), set(ac.CONFIG_FIELDS))
        self.assertEqual(len(ac._SNAKE_TO_CAMEL), len(ac._CAMEL_TO_SNAKE))

    def test_patch_model_fields_match_the_map(self):
        self.assertEqual(
            set(ac.CalibrationConfigPatch.__annotations__), set(ac._CAMEL_TO_SNAKE)
        )


class TestMergeAndValidate(unittest.TestCase):
    def test_empty_patch_rejected(self):
        _, errors = ac.merge_and_validate(dict(DEFAULTS), {})
        self.assertEqual(errors, ["no fields to update"])

    def test_valid_partial_merge_keeps_other_fields(self):
        merged, errors = ac.merge_and_validate(dict(DEFAULTS), {"min_agreement_rate": 0.95})
        self.assertEqual(errors, [])
        self.assertEqual(merged["min_agreement_rate"], 0.95)
        self.assertEqual(merged["min_sample_count"], DEFAULTS["min_sample_count"])

    def test_out_of_range_rejected_each_field(self):
        cases = {
            "min_agreement_rate": 0.5,
            "min_sample_count": 3,
            "min_promote_accuracy": 0.5,
            "min_rollback_accuracy": 0.2,
            "initial_canary_pct": 80.0,
            "novelty_threshold": 0.99,
        }
        for name, bad in cases.items():
            with self.subTest(name):
                _, errors = ac.merge_and_validate(dict(DEFAULTS), {name: bad})
                self.assertEqual(len(errors), 1, errors)

    def test_typo_like_095_as_0095_is_caught(self):
        _, errors = ac.merge_and_validate(dict(DEFAULTS), {"min_promote_accuracy": 0.095})
        self.assertTrue(errors)

    def test_bounds_are_inclusive(self):
        for name, (lo, hi, kind) in ac.BOUNDS.items():
            for edge in (lo, hi):
                with self.subTest(name=name, edge=edge):
                    cur = dict(DEFAULTS)
                    # widest valid gap so the pair check never masks a bound check
                    cur["min_promote_accuracy"], cur["min_rollback_accuracy"] = 1.0, 0.50
                    value = int(edge) if kind == "int" else float(edge)
                    _, errors = ac.merge_and_validate(cur, {name: value})
                    self.assertEqual(errors, [], errors)

    def test_bool_is_not_a_number(self):
        _, errors = ac.merge_and_validate(dict(DEFAULTS), {"min_sample_count": True})
        self.assertTrue(errors)

    def test_string_number_rejected(self):
        _, errors = ac.merge_and_validate(dict(DEFAULTS), {"min_agreement_rate": "0.9"})
        self.assertTrue(errors)

    def test_fractional_int_rejected(self):
        _, errors = ac.merge_and_validate(dict(DEFAULTS), {"min_sample_count": 30.5})
        self.assertTrue(errors)

    def test_integral_float_accepted_as_int(self):
        merged, errors = ac.merge_and_validate(dict(DEFAULTS), {"min_sample_count": 40.0})
        self.assertEqual(errors, [])
        self.assertEqual(merged["min_sample_count"], 40)
        self.assertIsInstance(merged["min_sample_count"], int)

    def test_logging_flag_must_be_bool(self):
        _, errors = ac.merge_and_validate(dict(DEFAULTS), {"novelty_logging_enabled": 1})
        self.assertTrue(errors)
        merged, errors = ac.merge_and_validate(dict(DEFAULTS), {"novelty_logging_enabled": False})
        self.assertEqual(errors, [])
        self.assertIs(merged["novelty_logging_enabled"], False)

    def test_unknown_field_rejected(self):
        _, errors = ac.merge_and_validate(dict(DEFAULTS), {"bogus": 1})
        self.assertEqual(errors, ["unknown field: bogus"])

    def test_rollback_must_be_below_promote_using_stored_value(self):
        # only rollback patched; promote comes from current
        _, errors = ac.merge_and_validate(dict(DEFAULTS), {"min_rollback_accuracy": 0.96})
        self.assertTrue(any("lower than" in e for e in errors), errors)

    def test_promote_lowered_below_stored_rollback_rejected(self):
        cur = dict(DEFAULTS, min_rollback_accuracy=0.90)
        _, errors = ac.merge_and_validate(cur, {"min_promote_accuracy": 0.85})
        self.assertTrue(any("lower than" in e for e in errors), errors)

    def test_equal_promote_and_rollback_rejected(self):
        _, errors = ac.merge_and_validate(
            dict(DEFAULTS), {"min_promote_accuracy": 0.9, "min_rollback_accuracy": 0.9}
        )
        self.assertTrue(errors)

    def test_pair_check_skipped_when_neither_patched(self):
        # legacy out-of-order values in the DB must not block unrelated edits
        cur = dict(DEFAULTS, min_promote_accuracy=0.80, min_rollback_accuracy=0.90)
        merged, errors = ac.merge_and_validate(cur, {"novelty_threshold": 0.4})
        self.assertEqual(errors, [])
        self.assertEqual(merged["novelty_threshold"], 0.4)

    def test_untouched_out_of_range_stored_value_does_not_block(self):
        cur = dict(DEFAULTS, min_agreement_rate=0.5)  # set by hand earlier
        _, errors = ac.merge_and_validate(cur, {"novelty_threshold": 0.4})
        self.assertEqual(errors, [])

    def test_all_errors_reported_together(self):
        _, errors = ac.merge_and_validate(
            dict(DEFAULTS), {"min_agreement_rate": 5, "novelty_threshold": 5}
        )
        self.assertEqual(len(errors), 2)

    def test_does_not_mutate_input(self):
        cur = dict(DEFAULTS)
        ac.merge_and_validate(cur, {"min_agreement_rate": 0.95})
        self.assertEqual(cur, DEFAULTS)


class TestGetHandler(unittest.TestCase):
    def test_tenant_missing_is_404(self):
        db = ScriptedSession([None])
        with self.assertRaises(HTTPException) as cm:
            run(ac.get_calibration_config(99, db))
        self.assertEqual(cm.exception.status_code, 404)

    def test_no_row_returns_defaults_flagged(self):
        db = ScriptedSession([SimpleNamespace(ok=1), None])
        out = run(ac.get_calibration_config(1, db))
        self.assertTrue(out["isDefault"])
        self.assertIsNone(out["updatedAt"])
        self.assertEqual(out["config"]["minAgreementRate"], 0.90)
        self.assertIn("minAgreementRate", out["bounds"])

    def test_stored_row_returned(self):
        db = ScriptedSession(
            [SimpleNamespace(ok=1), stored_row(min_agreement_rate=0.95, min_sample_count=50)]
        )
        out = run(ac.get_calibration_config(1, db))
        self.assertFalse(out["isDefault"])
        self.assertEqual(out["config"]["minAgreementRate"], 0.95)
        self.assertEqual(out["config"]["minSampleCount"], 50)
        self.assertEqual(out["updatedAt"], NOW.isoformat())

    def test_db_error_propagates_not_silently_defaults(self):
        class Boom(ScriptedSession):
            async def execute(self, q, p=None):
                raise RuntimeError("db down")

        with self.assertRaises(RuntimeError):
            run(ac.get_calibration_config(1, Boom([])))


def body(**kw):
    return ac.CalibrationConfigPatch(**kw)


class TestPutHandler(unittest.TestCase):
    def test_tenant_missing_is_404_and_nothing_written(self):
        db = ScriptedSession([None])
        with self.assertRaises(HTTPException) as cm:
            run(ac.put_calibration_config(99, body(minAgreementRate=0.95), db))
        self.assertEqual(cm.exception.status_code, 404)
        self.assertEqual(db.commits, 0)

    def test_first_write_creates_row_from_defaults_plus_patch(self):
        # exists, no stored row, (save), re-read after write
        after = stored_row(min_agreement_rate=0.95)
        db = ScriptedSession([SimpleNamespace(ok=1), None, after])
        out = run(ac.put_calibration_config(1, body(minAgreementRate=0.95), db))
        writes = [(s, p) for s, p in db.executed if "INSERT INTO tenant_calibration_config" in s]
        self.assertEqual(len(writes), 1)
        params = writes[0][1]
        self.assertEqual(params["tenant_id"], 1)
        self.assertEqual(params["min_agreement_rate"], 0.95)
        self.assertEqual(params["min_sample_count"], 30)  # default preserved
        self.assertEqual(db.commits, 1)
        self.assertFalse(out["isDefault"])
        self.assertEqual(out["config"]["minAgreementRate"], 0.95)

    def test_partial_update_preserves_stored_values(self):
        cur = stored_row(min_agreement_rate=0.93, min_sample_count=80)
        after = stored_row(min_agreement_rate=0.93, min_sample_count=80, novelty_threshold=0.40)
        db = ScriptedSession([SimpleNamespace(ok=1), cur, after])
        run(ac.put_calibration_config(1, body(noveltyThreshold=0.40), db))
        params = [p for s, p in db.executed if "INSERT INTO tenant_calibration_config" in s][0]
        self.assertEqual(params["min_agreement_rate"], 0.93)   # NOT reset to default
        self.assertEqual(params["min_sample_count"], 80)
        self.assertEqual(params["novelty_threshold"], 0.40)

    def test_null_field_is_not_provided(self):
        cur = stored_row(min_sample_count=80)
        after = stored_row(min_sample_count=80, novelty_threshold=0.40)
        db = ScriptedSession([SimpleNamespace(ok=1), cur, after])
        run(ac.put_calibration_config(1, body(noveltyThreshold=0.40, minSampleCount=None), db))
        params = [p for s, p in db.executed if "INSERT INTO tenant_calibration_config" in s][0]
        self.assertEqual(params["min_sample_count"], 80)

    def test_validation_failure_is_422_and_writes_nothing(self):
        db = ScriptedSession([SimpleNamespace(ok=1), stored_row()])
        with self.assertRaises(HTTPException) as cm:
            run(ac.put_calibration_config(1, body(minPromoteAccuracy=0.5), db))
        self.assertEqual(cm.exception.status_code, 422)
        self.assertIn("errors", cm.exception.detail)
        self.assertEqual(db.commits, 0)
        self.assertFalse([s for s, _ in db.executed if "INSERT" in s])

    def test_empty_body_is_422(self):
        db = ScriptedSession([SimpleNamespace(ok=1), stored_row()])
        with self.assertRaises(HTTPException) as cm:
            run(ac.put_calibration_config(1, body(), db))
        self.assertEqual(cm.exception.status_code, 422)

    def test_db_error_on_read_does_not_overwrite_with_defaults(self):
        class FailOnConfigRead(ScriptedSession):
            async def execute(self, q, p=None):
                if "FROM tenant_calibration_config" in str(q):
                    raise RuntimeError("db down")
                return await super().execute(q, p)

        db = FailOnConfigRead([SimpleNamespace(ok=1)])
        with self.assertRaises(RuntimeError):
            run(ac.put_calibration_config(1, body(minAgreementRate=0.95), db))
        self.assertFalse([s for s, _ in db.executed if "INSERT" in s])

    def test_all_writes_are_tenant_scoped_by_path_id(self):
        db = ScriptedSession([SimpleNamespace(ok=1), None, stored_row()])
        run(ac.put_calibration_config(7, body(minAgreementRate=0.95), db))
        for sql, params in db.executed:
            if params is not None:
                self.assertEqual(params.get("tenant_id"), 7)


class TestRouterWiring(unittest.TestCase):
    def test_routes_registered(self):
        got = {(m, p) for m, p, _ in ac.router.routes}
        self.assertIn(("GET", "/tenants/{tenant_id}/calibration-config"), got)
        self.assertIn(("PUT", "/tenants/{tenant_id}/calibration-config"), got)

    def test_router_is_secret_gated_and_prefixed(self):
        self.assertEqual(ac.router.prefix, "/admin")
        self.assertTrue(ac.router.dependencies)

    def test_config_type_still_saveable_with_merged_values(self):
        merged, errors = ac.merge_and_validate(dict(DEFAULTS), {"min_agreement_rate": 0.95})
        self.assertEqual(errors, [])
        cfg = TenantCalibrationConfig(tenant_id=1, **merged)
        self.assertEqual(cfg.min_agreement_rate, 0.95)


if __name__ == "__main__":
    unittest.main()
