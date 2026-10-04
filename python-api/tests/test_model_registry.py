"""Tests for app/language/model_registry.py (Phase 6). Fake session.
    docker compose exec python-api python -m unittest tests.test_model_registry -v
"""
import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests import _sa_stub  # noqa: F401
from app.language import model_registry as mr
from app.language.intent_model import IntentModel
from tests.test_intent_model import blobs


class FakeDB:
    def __init__(self, row=None):
        self.row, self.calls, self.commits = row, [], 0

    async def execute(self, q, params=None):
        self.calls.append((str(q), params or {}))
        row = self.row
        return SimpleNamespace(first=lambda: row)

    async def commit(self):
        self.commits += 1


def run(c):
    return asyncio.run(c)


class TestStateMachine(unittest.TestCase):
    def test_happy_path(self):
        for a, b in (("trained", "shadow"), ("shadow", "canary"), ("canary", "active"), ("active", "retired")):
            mr.validate_transition(a, b)

    def test_cannot_skip_shadow_or_canary(self):
        for a, b in (("trained", "canary"), ("trained", "active"), ("shadow", "active")):
            with self.assertRaises(mr.RegistryError):
                mr.validate_transition(a, b)

    def test_terminal_states_are_terminal(self):
        for s in ("retired", "rejected"):
            for t in mr.STATUSES:
                with self.assertRaises(mr.RegistryError):
                    mr.validate_transition(s, t)

    def test_active_cannot_go_back(self):
        with self.assertRaises(mr.RegistryError):
            mr.validate_transition("active", "shadow")

    def test_unknown_names(self):
        with self.assertRaises(mr.RegistryError):
            mr.validate_transition("x", "shadow")
        with self.assertRaises(mr.RegistryError):
            mr.validate_transition("trained", "y")

    def test_sql_check_lists_same_statuses(self):
        sql = (Path(__file__).parents[2] / "db/init/020_language_models.sql").read_text()
        for s in mr.STATUSES:
            self.assertIn(f"'{s}'", sql)
        self.assertIn("uq_language_models_one_active", sql)


class TestShadowGate(unittest.TestCase):
    def test_only_passing_eval_enters_shadow(self):
        self.assertTrue(mr.can_enter_shadow({"ok": True}))
        for bad in ({"ok": False}, {}, None, "x", {"ok": "yes"}):
            self.assertFalse(mr.can_enter_shadow(bad))


class TestSetStatus(unittest.TestCase):
    def test_failed_eval_cannot_enter_shadow(self):
        db = FakeDB(SimpleNamespace(status="trained", eval_report={"ok": False}))
        with self.assertRaises(mr.RegistryError):
            run(mr.set_status(db, tenant_id=1, version="v", target="shadow", reason="try"))
        self.assertEqual(db.commits, 0)

    def test_eval_report_as_json_string_is_handled(self):
        db = FakeDB(SimpleNamespace(status="trained", eval_report=json.dumps({"ok": True})))
        run(mr.set_status(db, tenant_id=1, version="v", target="shadow", reason="passed"))
        self.assertEqual(db.commits, 1)

    def test_reason_required(self):
        db = FakeDB(SimpleNamespace(status="shadow", eval_report={"ok": True}))
        with self.assertRaises(mr.RegistryError):
            run(mr.set_status(db, tenant_id=1, version="v", target="canary", reason="  "))

    def test_missing_model(self):
        with self.assertRaises(mr.RegistryError):
            run(mr.set_status(FakeDB(None), tenant_id=1, version="v", target="shadow", reason="r"))

    def test_activation_retires_previous_active_first_and_is_tenant_scoped(self):
        db = FakeDB(SimpleNamespace(status="canary", eval_report={"ok": True}))
        run(mr.set_status(db, tenant_id=7, version="v2", target="active", reason="canary ok"))
        sqls = [c[0] for c in db.calls]
        self.assertIn("status='retired'", sqls[1].replace(" ", ""))
        self.assertLess(1, len(sqls))
        for _, p in db.calls:
            self.assertEqual(p.get("t"), 7)
        self.assertEqual(db.commits, 1)

    def test_illegal_move_writes_nothing(self):
        db = FakeDB(SimpleNamespace(status="trained", eval_report={"ok": True}))
        with self.assertRaises(mr.RegistryError):
            run(mr.set_status(db, tenant_id=1, version="v", target="active", reason="skip"))
        self.assertEqual(len(db.calls), 1)  # only the SELECT


class TestRegister(unittest.TestCase):
    def setUp(self):
        self.model = IntentModel().fit(*blobs())

    def reg(self, db, **kw):
        args = dict(tenant_id=3, model=self.model, embedding_model="emb", dataset_fingerprint="a" * 64,
                    train_counts={"train": 1, "val": 1, "test": 1}, eval_report={"ok": False, "failures": ["x"]})
        args.update(kw)
        return run(mr.register_model(db, **args))

    def test_registers_as_trained_only(self):
        db = FakeDB()
        v = self.reg(db)
        sql, p = db.calls[0]
        self.assertIn("'trained'", sql)
        self.assertEqual(p["tenant_id"], 3)
        self.assertEqual(p["sha"], self.model.fingerprint())
        self.assertTrue(v.startswith("intent-lr-"))
        self.assertEqual(db.commits, 1)

    def test_artifact_stored_has_no_text(self):
        db = FakeDB(); self.reg(db)
        self.assertEqual(set(json.loads(db.calls[0][1]["artifact"])), {"kind", "labels", "dim", "temperature", "trained_on", "W", "b"})

    def test_bad_fingerprint_rejected(self):
        with self.assertRaises(mr.RegistryError):
            self.reg(FakeDB(), dataset_fingerprint="short")


if __name__ == "__main__":
    unittest.main()
