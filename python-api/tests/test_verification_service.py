"""
Tests for verification_service (§9 item 2 follow-up: human_confirmed /
human_corrected write paths).

Run:
    docker compose exec python-api python -m unittest tests.test_verification_service -v
"""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from app.language import verification_service


EXP_ID = str(uuid.uuid4())
TENANT_ID = 1


class FakeExecResult:
    """Result of an UPDATE — only rowcount is used by confirm_experience."""

    def __init__(self, rowcount):
        self.rowcount = rowcount


class FakeSelectResult:
    """Result of the SELECT in correct_experience — only .first() is used."""

    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class FakeSession:
    """Records every statement executed (truncated) and commit count.
    `responses` is a list consumed in order, one per execute() call, so a
    test can script "SELECT returns this row, then the UPDATE returns
    this rowcount" without a real query planner."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.executed = []
        self.commits = 0

    async def execute(self, q, p=None):
        self.executed.append((str(q)[:40], p))
        return self.responses.pop(0)

    async def commit(self):
        self.commits += 1


def _row(**overrides):
    base = dict(
        customer_id=5,
        conversation_id=9,
        message_id=42,
        request_id="req-1",
        channel="whatsapp",
        original_message="amar parcel ta koi?",
        detected_language="bn",
        reply_language="bn",
        normalized_message="amar parcel ta koi",
        communication_style="casual",
        script="latin",
        is_transliterated=True,
        transliterated_from="bengali",
        code_mixing="none",
        language="bn",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestConfirmExperience(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_updates_and_commits_when_row_exists(self):
        session = FakeSession([FakeExecResult(rowcount=1)])
        ok = await verification_service.confirm_experience(session, TENANT_ID, EXP_ID)
        self.assertTrue(ok)
        self.assertEqual(session.commits, 1)

    async def test_confirm_returns_false_when_no_row_matches(self):
        session = FakeSession([FakeExecResult(rowcount=0)])
        ok = await verification_service.confirm_experience(session, TENANT_ID, EXP_ID)
        self.assertFalse(ok)
        # Still commits (a no-op UPDATE is harmless) but the caller sees False.
        self.assertEqual(session.commits, 1)

    async def test_confirm_is_tenant_scoped(self):
        session = FakeSession([FakeExecResult(rowcount=1)])
        await verification_service.confirm_experience(session, TENANT_ID, EXP_ID)
        _, params = session.executed[0]
        self.assertEqual(params["tenant_id"], TENANT_ID)
        self.assertEqual(params["experience_id"], EXP_ID)


class TestCorrectExperience(unittest.IsolatedAsyncioTestCase):
    async def test_correct_returns_none_when_row_not_found(self):
        session = FakeSession([FakeSelectResult(None)])
        result = await verification_service.correct_experience(
            session, TENANT_ID, EXP_ID, "ORDER_STATUS"
        )
        self.assertIsNone(result)
        # Nothing else should have been attempted.
        self.assertEqual(len(session.executed), 1)
        self.assertEqual(session.commits, 0)

    async def test_correct_inserts_new_row_and_supersedes_old(self):
        session = FakeSession(
            [
                FakeSelectResult(_row()),  # SELECT the original row
                FakeExecResult(rowcount=1),  # the INSERT inside insert_experience_row
                FakeExecResult(rowcount=1),  # UPDATE superseded_by
            ]
        )
        new_id = await verification_service.correct_experience(
            session, TENANT_ID, EXP_ID, "ORDER_STATUS"
        )
        self.assertIsNotNone(new_id)
        self.assertNotEqual(new_id, EXP_ID)
        # SELECT, INSERT, UPDATE — three statements, in that order.
        self.assertEqual(len(session.executed), 3)
        self.assertIn("INSERT INTO language_experi", session.executed[1][0])
        self.assertIn("UPDATE", session.executed[2][0])
        # The UPDATE must point superseded_by at the freshly inserted id.
        update_params = session.executed[2][1]
        self.assertEqual(update_params["new_id"], new_id)
        self.assertEqual(update_params["experience_id"], EXP_ID)
        self.assertEqual(update_params["tenant_id"], TENANT_ID)
        # insert_experience_row + the final UPDATE each commit once.
        self.assertEqual(session.commits, 2)

    async def test_correct_carries_over_original_turn_fields(self):
        original = _row(channel="facebook", script="bengali", is_transliterated=False, language="bn,en")
        session = FakeSession(
            [
                FakeSelectResult(original),
                FakeExecResult(rowcount=1),
                FakeExecResult(rowcount=1),
            ]
        )
        await verification_service.correct_experience(session, TENANT_ID, EXP_ID, "PRICE_INQUIRY")
        insert_params = session.executed[1][1]
        self.assertEqual(insert_params["customer_id"], original.customer_id)
        self.assertEqual(insert_params["conversation_id"], original.conversation_id)
        self.assertEqual(insert_params["channel"], "facebook")
        self.assertEqual(insert_params["script"], "bengali")
        self.assertEqual(insert_params["is_transliterated"], False)
        self.assertEqual(insert_params["language"], "bn,en")
        self.assertEqual(insert_params["final_intent"], "PRICE_INQUIRY")
        self.assertEqual(insert_params["verification_level"], "human_corrected")

    async def test_correct_propagates_insert_failure_instead_of_swallowing(self):
        """Unlike record_language_experience's isolated write, a human
        correction must fail loudly if the INSERT fails."""

        class BoomSession(FakeSession):
            async def execute(self, q, p=None):
                if "INSERT INTO language_experiences" in str(q):
                    raise RuntimeError("db down")
                return await super().execute(q, p)

        session = BoomSession([FakeSelectResult(_row())])
        with self.assertRaises(RuntimeError):
            await verification_service.correct_experience(
                session, TENANT_ID, EXP_ID, "ORDER_STATUS"
            )


if __name__ == "__main__":
    unittest.main()
