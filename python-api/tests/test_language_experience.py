"""
Tests for the Language Experience Store write path (Phase 0 of the
Language Intelligence design doc) -- app/language/experience_service.py
+ experience_types.py.

Same style as tests/test_agent_foundation.py's TestAgentRunTrace: a
fake in-memory session satisfying just the execute()/commit() shape
needed, not a real Postgres connection. Unit tests of the Python
write-path logic, not an integration test.
"""

import unittest
import uuid

from app.language.experience_service import record_language_experience
from app.language.experience_types import LanguageExperience


class FakeSession:
    def __init__(self):
        self.executed_queries = []
        self.commit_count = 0

    async def execute(self, query, params=None):
        self.executed_queries.append((query, params))
        return None

    async def commit(self):
        self.commit_count += 1


class FailingSession(FakeSession):
    async def execute(self, query, params=None):
        self.executed_queries.append((query, params))
        raise RuntimeError("db temporarily unavailable")


class TestLanguageExperience(unittest.IsolatedAsyncioTestCase):
    async def test_records_and_commits(self):
        session = FakeSession()
        experience = LanguageExperience(
            experience_id=uuid.uuid4(),
            tenant_id=1,
            customer_id=2,
            conversation_id=3,
            message_id=4,
            detected_language="mixed",
            reply_language="bn",
            normalized_message="how much is this product",
            communication_style="informal",
            is_ambiguous=False,
            entity_spans=["Nike Air Max"],
            llm_raw_confidence=0.87,
            llm_model_version="claude-sonnet-5",
            final_intent="PRICE_INQUIRY",
        )

        await record_language_experience(session, experience)

        self.assertEqual(len(session.executed_queries), 1)
        self.assertEqual(session.commit_count, 1)
        _, params = session.executed_queries[0]
        self.assertEqual(params["tenant_id"], 1)
        self.assertEqual(params["conversation_id"], 3)
        self.assertEqual(params["detected_language"], "mixed")
        self.assertEqual(params["entity_spans"], ["Nike Air Max"])
        self.assertEqual(params["llm_raw_confidence"], 0.87)

    async def test_defaults_match_phase_0_scope(self):
        """
        Phase 0 has no own Language Brain and is 100% LLM-driven --
        this pins those defaults so a future change can't silently
        start claiming brain usage or auto-verified outcomes without a
        deliberate, reviewed change to LanguageExperience itself.
        """
        session = FakeSession()
        experience = LanguageExperience(
            experience_id=uuid.uuid4(),
            tenant_id=1,
            customer_id=2,
            conversation_id=3,
        )

        await record_language_experience(session, experience)

        _, params = session.executed_queries[0]
        self.assertFalse(params["brain_used"])
        self.assertIsNone(params["brain_prediction"])
        self.assertIsNone(params["brain_version"])
        self.assertTrue(params["llm_called"])
        self.assertEqual(params["verification_result"], "unverified")
        self.assertTrue(params["learning_eligible"])

    async def test_business_risk_intents_can_be_marked_learning_ineligible(self):
        """
        CoreAgent, not this module, decides learning_eligible (see
        core_agent.py's _LEARNING_INELIGIBLE_INTENTS) -- this test only
        confirms the flag actually reaches the bound query params when
        set to False, since that's the whole point of storing it.
        """
        session = FakeSession()
        experience = LanguageExperience(
            experience_id=uuid.uuid4(),
            tenant_id=1,
            customer_id=2,
            conversation_id=3,
            final_intent="CREATE_ORDER",
            learning_eligible=False,
        )

        await record_language_experience(session, experience)

        _, params = session.executed_queries[0]
        self.assertFalse(params["learning_eligible"])

    async def test_write_failure_is_isolated_and_never_raises(self):
        session = FailingSession()
        experience = LanguageExperience(
            experience_id=uuid.uuid4(), tenant_id=1, customer_id=2, conversation_id=3
        )

        try:
            await record_language_experience(session, experience)
        except Exception as exc:
            self.fail(f"record_language_experience() raised unexpectedly: {exc}")


if __name__ == "__main__":
    unittest.main()
