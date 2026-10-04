"""
Tests for P5-4a: persisting the ADVISORY TurnPlan next to turn_understandings
(migration 026). Fake session, no Postgres.

Run:
    docker compose exec python-api python -m unittest tests.test_turn_plan_persistence -v
"""

import re
import unittest
import uuid
from pathlib import Path

from tests import _sa_stub  # noqa: F401

from app.language import glb
from app.language.glb_orchestrator import plan_to_dict, plan_turn
from app.language.understanding_store import (
    _PLAN_KEYS, plan_to_params, record_turn_understanding,
)

ROOT = Path(__file__).resolve().parents[2]
ARGS = dict(tenant_id=1, customer_id=2, conversation_id=3, message_id=4,
            request_id="r1", experience_id=uuid.uuid4())


class FakeSession:
    """fail_when: substring; a statement containing it raises."""
    def __init__(self, fail_when=None):
        self.calls, self.commits, self.rollbacks = [], 0, 0
        self._fail_when = fail_when

    async def execute(self, q, p=None):
        sql = str(q)
        self.calls.append((sql, p))
        if self._fail_when and self._fail_when in sql:
            raise RuntimeError("column does not exist")

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _u(**over):
    return glb.understanding_to_dict(glb.assemble_turn_understanding(
        language="bn", script="latin", is_transliterated=True,
        normalized_message="SECRET TEXT", intent="PRICE_INQUIRY",
        intent_confidence=0.9, intent_source="llm", **over))


def _plan(**over):
    return plan_to_dict(plan_turn(_u(**over)))


class TestPlanToParams(unittest.TestCase):
    def test_none_and_empty_give_none(self):
        self.assertIsNone(plan_to_params(None))
        self.assertIsNone(plan_to_params({}))

    def test_maps_every_plan_column(self):
        p = plan_to_params(_plan(is_novel=True))
        self.assertEqual(set(p), set(_PLAN_KEYS))
        self.assertEqual(p["plan_understanding_source"], "llm_teacher_required")
        self.assertTrue(p["plan_triage"])
        self.assertIn("triage:novel", p["plan_reasons"])

    def test_constants_are_not_stored(self):
        p = plan_to_params(_plan())
        self.assertNotIn("enacted", p)
        self.assertNotIn("advisory", p)
        self.assertFalse(any("enacted" in k for k in p))

    def test_no_message_text_in_params(self):
        self.assertNotIn("SECRET TEXT", repr(plan_to_params(_plan())))


class TestPersistence(unittest.IsolatedAsyncioTestCase):
    async def test_plan_columns_written_with_the_row(self):
        s = FakeSession()
        ok = await record_turn_understanding(s, understanding=_u(), plan=_plan(), **ARGS)
        self.assertTrue(ok)
        self.assertEqual(len(s.calls), 1)          # same row, one INSERT
        q, p = s.calls[0]
        for k in _PLAN_KEYS:
            self.assertIn(k, q)
            self.assertIn(k, p)
        self.assertEqual(s.commits, 1)

    async def test_without_plan_sql_has_no_plan_columns(self):
        s = FakeSession()
        await record_turn_understanding(s, understanding=_u(), **ARGS)
        self.assertNotIn("plan_", s.calls[0][0])

    async def test_missing_migration_falls_back_to_row_without_plan(self):
        # simulates code deployed before migration 026: any INSERT that
        # names plan_* columns fails; the plain one must still land.
        s = FakeSession(fail_when="plan_understanding_source")
        ok = await record_turn_understanding(s, understanding=_u(), plan=_plan(), **ARGS)
        self.assertTrue(ok)
        self.assertEqual(len(s.calls), 2)
        self.assertIn("plan_", s.calls[0][0])
        self.assertNotIn("plan_", s.calls[1][0])
        self.assertEqual(s.rollbacks, 1)
        self.assertEqual(s.commits, 1)

    async def test_total_failure_is_isolated(self):
        s = FakeSession(fail_when="INSERT")
        ok = await record_turn_understanding(s, understanding=_u(), plan=_plan(), **ARGS)
        self.assertFalse(ok)                        # never raises
        self.assertGreaterEqual(s.rollbacks, 2)

    async def test_no_plan_failure_does_not_retry(self):
        s = FakeSession(fail_when="INSERT")
        self.assertFalse(await record_turn_understanding(s, understanding=_u(), **ARGS))
        self.assertEqual(len(s.calls), 1)


class TestMigration026(unittest.TestCase):
    def setUp(self):
        self.sql = (ROOT / "db/init/026_turn_plan.sql").read_text(encoding="utf-8")

    def test_every_written_column_is_added(self):
        for k in _PLAN_KEYS:
            self.assertRegex(self.sql, rf"ADD COLUMN IF NOT EXISTS {k}\b", k)

    def test_is_idempotent_and_additive_only(self):
        body = "\n".join(l for l in self.sql.splitlines() if not l.strip().startswith("--")).upper()
        self.assertNotIn("DROP ", body)
        self.assertNotIn("NOT NULL", body)          # old rows must stay valid
        self.assertEqual(body.count("ALTER TABLE"), 1)

    def test_no_text_or_person_columns(self):
        body = "\n".join(l for l in self.sql.splitlines() if not l.strip().startswith("--")).lower()
        for bad in ("message", "nationality", "ethnic", "religion", "country", "location", "race"):
            self.assertNotIn(bad, body)

    def test_old_migration_018_untouched_by_plan_columns(self):
        old = (ROOT / "db/init/018_turn_understandings.sql").read_text(encoding="utf-8")
        self.assertNotIn("plan_", old)

    def test_migrate_script_includes_026(self):
        self.assertRegex((ROOT / "scripts/migrate.sh").read_text(), r"\b026\b")


class TestCoreAgentWiring(unittest.TestCase):
    def test_core_agent_passes_plan_to_the_store(self):
        src = (ROOT / "python-api/app/agent/core_agent.py").read_text(encoding="utf-8")
        m = re.search(r"await record_turn_understanding\((.*?)\n            \)", src, re.S)
        self.assertIsNotNone(m)
        self.assertIn("plan=plan", m.group(1))


if __name__ == "__main__":
    unittest.main()
