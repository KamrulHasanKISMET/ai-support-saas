"""
Tests for app/language/understanding_store.py (Phase 5 C1): metadata-only
persistence of TurnUnderstanding with 90-day retention. Fake session,
no Postgres -- unit tests of the Python logic, like the other stores.

Run:
    docker compose exec python-api python -m unittest tests.test_understanding_store -v
"""

import re
import unittest
import uuid
from pathlib import Path

from app.language import glb
from app.language.understanding_store import (
    PURGE_BATCH_SIZE, TURN_UNDERSTANDING_RETENTION_DAYS,
    insert_turn_understanding, purge_expired, record_turn_understanding,
)

ROOT = Path(__file__).resolve().parents[2]  # repo root


class Res:
    def __init__(self, rowcount=0, n=0):
        self.rowcount, self._n = rowcount, n
    def first(self):
        return type("R", (), {"n": self._n})()


class FakeSession:
    def __init__(self, delete_counts=(), count=0, fail=False):
        self.calls, self.commits, self.rollbacks = [], 0, 0
        self._deletes = list(delete_counts)
        self._count, self._fail = count, fail
    async def execute(self, q, p=None):
        if self._fail:
            raise RuntimeError("db down")
        self.calls.append((str(q), p))
        if "DELETE" in str(q):
            return Res(rowcount=self._deletes.pop(0) if self._deletes else 0)
        return Res(n=self._count)
    async def commit(self):
        self.commits += 1
    async def rollback(self):
        self.rollbacks += 1


def _u(**over):
    return glb.understanding_to_dict(glb.assemble_turn_understanding(
        language="bn,en", script="bengali", is_transliterated=True,
        code_mixing="intra_sentential", normalized_message="SECRET TEXT",
        intent="PRICE_INQUIRY", intent_confidence=0.9, intent_source="llm", **over))


ARGS = dict(tenant_id=1, customer_id=2, conversation_id=3, message_id=4,
            request_id="r1", experience_id=uuid.uuid4())


class TestInsert(unittest.IsolatedAsyncioTestCase):
    async def test_persists_fields_and_commits(self):
        s = FakeSession()
        ok = await record_turn_understanding(s, understanding=_u(), **ARGS)
        self.assertTrue(ok)
        q, p = s.calls[0]
        self.assertIn("INSERT INTO turn_understandings", q)
        self.assertEqual((p["language"], p["script"], p["intent"]), ("bn,en", "bengali", "PRICE_INQUIRY"))
        self.assertEqual(p["phenomena"], ["code_mixed", "transliterated"])
        self.assertEqual(p["retention_days"], 90)
        self.assertEqual(p["experience_id"], str(ARGS["experience_id"]))
        self.assertEqual(s.commits, 1)

    async def test_message_text_is_never_persisted(self):
        s = FakeSession()
        await record_turn_understanding(s, understanding=_u(), **ARGS)
        q, p = s.calls[0]
        self.assertNotIn("normalized_message", q)
        self.assertNotIn("SECRET TEXT", repr(p))

    async def test_failure_is_isolated_and_rolls_back(self):
        s = FakeSession(fail=True)
        ok = await record_turn_understanding(s, understanding=_u(), **ARGS)
        self.assertFalse(ok)
        self.assertEqual(s.rollbacks, 1)

    async def test_empty_understanding_is_noop(self):
        s = FakeSession()
        self.assertFalse(await record_turn_understanding(s, understanding=None, **ARGS))
        self.assertEqual(s.calls, [])

    async def test_bare_insert_raises(self):
        with self.assertRaises(RuntimeError):
            await insert_turn_understanding(FakeSession(fail=True), understanding=_u(), **ARGS)

    async def test_missing_optional_ids_ok(self):
        s = FakeSession()
        args = dict(ARGS, message_id=None, request_id=None, experience_id=None)
        self.assertTrue(await record_turn_understanding(s, understanding=_u(), **args))
        self.assertIsNone(s.calls[0][1]["experience_id"])


class TestPurge(unittest.IsolatedAsyncioTestCase):
    async def test_batches_until_short_batch(self):
        s = FakeSession(delete_counts=[PURGE_BATCH_SIZE, PURGE_BATCH_SIZE, 17])
        n = await purge_expired(s)
        self.assertEqual(n, 2 * PURGE_BATCH_SIZE + 17)
        self.assertEqual(s.commits, 3)

    async def test_nothing_expired(self):
        s = FakeSession(delete_counts=[0])
        self.assertEqual(await purge_expired(s), 0)

    async def test_dry_run_deletes_nothing(self):
        s = FakeSession(count=42)
        self.assertEqual(await purge_expired(s, dry_run=True), 42)
        self.assertNotIn("DELETE", s.calls[0][0])
        self.assertEqual(s.commits, 0)

    async def test_purge_fails_loudly(self):
        with self.assertRaises(RuntimeError):
            await purge_expired(FakeSession(fail=True))

    async def test_only_expired_rows_targeted(self):
        s = FakeSession(delete_counts=[0])
        await purge_expired(s)
        self.assertIn("expires_at < CURRENT_TIMESTAMP", s.calls[0][0])


class TestMigrationAgreesWithCode(unittest.TestCase):
    def setUp(self):
        self.sql = (ROOT / "db/init/018_turn_understandings.sql").read_text(encoding="utf-8")

    def test_retention_is_90_days_in_code(self):
        self.assertEqual(TURN_UNDERSTANDING_RETENTION_DAYS, 90)

    def test_sql_default_matches_code_constant(self):
        m = re.search(r"INTERVAL '(\d+) days'", self.sql)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), TURN_UNDERSTANDING_RETENTION_DAYS)

    def test_every_written_column_exists_in_table(self):
        from app.language.understanding_store import _INSERT_KEYS
        for k in _INSERT_KEYS:
            self.assertRegex(self.sql, rf"\b{k}\b", k)

    def test_table_has_no_text_or_person_columns(self):
        body = self.sql.split("CREATE TABLE", 1)[1].split(");", 1)[0].lower()
        for bad in ("normalized_message", "original_message", "nationality", "ethnic",
                    "religion", "country", "location", "geo", "race"):
            self.assertNotIn(bad, body)

    def test_cascade_from_tenant(self):
        self.assertIn("REFERENCES tenants(id) ON DELETE CASCADE", self.sql)

    def test_columns_cover_all_persistable_understanding_fields(self):
        # every TurnUnderstanding field is stored except the text.
        from app.language.understanding_store import _INSERT_KEYS
        self.assertEqual(set(glb.field_names()) - set(_INSERT_KEYS), {"normalized_message"})


if __name__ == "__main__":
    unittest.main()
