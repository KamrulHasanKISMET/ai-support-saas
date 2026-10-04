"""
Tests for ConversationCostTracker (Phase 4).

Run:
    docker compose exec python-api python -m unittest tests.test_conversation_cost_tracker -v
"""
import unittest
from app.language.conversation_cost_tracker import ConversationCostTracker


class FakeResult:
    def first(self): return None


class FakeSession:
    def __init__(self):
        self.last_params = {}
        self.commits = 0
    async def execute(self, query, params=None):
        self.last_params = params or {}
        return FakeResult()
    async def commit(self):
        self.commits += 1


class FailingSession(FakeSession):
    async def execute(self, query, params=None):
        raise RuntimeError("db down")


class TestConversationCostTracker(unittest.IsolatedAsyncioTestCase):

    async def _record(self, session, **kwargs):
        defaults = dict(
            tenant_id=1, conversation_id=10,
            input_tokens=100, output_tokens=50,
            intent_source="llm", is_resolved=False,
        )
        defaults.update(kwargs)
        tracker = ConversationCostTracker()
        await tracker.record(session, **defaults)

    async def test_llm_turn_brain_saved_zero(self):
        session = FakeSession()
        await self._record(session, intent_source="llm")
        self.assertEqual(session.last_params.get("brain_saved"), 0)
        self.assertGreater(session.commits, 0)

    async def test_brain_turn_increments_saved(self):
        session = FakeSession()
        await self._record(session, intent_source="brain")
        self.assertEqual(session.last_params.get("brain_saved"), 1)

    async def test_none_intent_source_not_saved(self):
        session = FakeSession()
        await self._record(session, intent_source=None)
        self.assertEqual(session.last_params.get("brain_saved"), 0)

    async def test_none_tokens_treated_as_zero(self):
        session = FakeSession()
        await self._record(session, input_tokens=None, output_tokens=None)
        self.assertEqual(session.last_params.get("input_tokens"), 0)
        self.assertEqual(session.last_params.get("output_tokens"), 0)

    async def test_resolved_flag_passed(self):
        session = FakeSession()
        await self._record(session, is_resolved=True)
        self.assertTrue(session.last_params.get("is_resolved"))

    async def test_db_failure_never_raises(self):
        session = FailingSession()
        try:
            await self._record(session)
        except Exception as e:
            self.fail(f"record() raised: {e}")

    async def test_commits_on_success(self):
        session = FakeSession()
        await self._record(session)
        self.assertEqual(session.commits, 1)


if __name__ == "__main__":
    unittest.main()
