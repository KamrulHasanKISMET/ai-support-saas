"""
CoreAgent.run() integration test (all collaborators mocked) for the
Phase 5 additions: metadata["understanding"] and the novelty -> 
TurnUnderstanding.is_novel wiring (docs/PENDING_WORK.md C2).

Proves: the record is built from the Kernel result, `novel` appears
when NoveltyDetector says so (even with logging disabled), the reply is
untouched, and a failure while assembling the record can never break
the reply.

Run:
    docker compose exec python-api python -m unittest tests.test_core_agent_understanding -v
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.agent import core_agent as ca
from app.language.novelty_detector import NoveltyOutcome
from app.schemas.agent import AgentRequest
from app.schemas.kernel import KernelRunResponse


def _kernel_result(**over):
    base = dict(
        reply="৫০০ টাকা", intent="PRICE_INQUIRY", confidence=0.93,
        detectedLanguage="mixed", replyLanguage="bn",
        normalizedMessage="price of this?", intentSource="llm",
        language="bn,en", script="bengali", isTransliterated=True,
        codeMixing="intra_sentential", isAmbiguous=False,
    )
    base.update(over)
    return KernelRunResponse(**base)


class TestCoreAgentUnderstanding(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.persist = AsyncMock(return_value=True)

    async def _run(self, kernel_result, outcome=NoveltyOutcome(False, False), glb_raises=False):
        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        routing = MagicMock(routed_to="llm", predicted_intent=None, similarity=None,
                            calibrated_threshold=None)
        patches = [
            patch.object(ca, "load_agent_config", AsyncMock(return_value=ca.DEFAULT_AGENT_CONFIG)),
            patch.object(ca.shadow_brain, "predict", AsyncMock(return_value=None)),
            patch.object(ca.routing_service, "decide", AsyncMock(return_value=routing)),
            patch.object(ca.kernel, "run", AsyncMock(return_value=kernel_result)),
            patch.object(ca, "record_trace", AsyncMock()),
            patch.object(ca, "record_language_experience", AsyncMock()),
            patch.object(ca, "record_turn_understanding", self.persist),
            patch.object(ca, "_record_routing_decision", AsyncMock()),
            patch.object(ca.novelty_detector, "check", AsyncMock(return_value=outcome)),
            patch.object(ca.conversation_cost_tracker, "record", AsyncMock()),
        ]
        if glb_raises:
            patches.append(patch.object(ca.glb, "assemble_turn_understanding", side_effect=RuntimeError("boom")))
        for p in patches:
            p.start()
        try:
            req = AgentRequest(tenantId=1, customerId=2, conversationId=3, messageId=4, message="dam koto?")
            return await ca.CoreAgent().run(db, req)
        finally:
            for p in patches:
                p.stop()

    async def test_understanding_reflects_kernel_result(self):
        resp = await self._run(_kernel_result())
        u = resp.metadata["understanding"]
        self.assertEqual(u["language"], "bn,en")
        self.assertEqual(u["script"], "bengali")
        self.assertEqual(u["intent"], "PRICE_INQUIRY")
        self.assertEqual(u["intent_source"], "llm")
        self.assertEqual(u["phenomena"], ["code_mixed", "transliterated"])
        self.assertTrue(u["automation_eligible"])

    async def test_order_status_not_automatable_in_record(self):
        resp = await self._run(_kernel_result(intent="ORDER_STATUS"))
        u = resp.metadata["understanding"]
        self.assertFalse(u["automation_eligible"])
        self.assertTrue(u["language_learning_eligible"])

    async def test_novel_turn_flagged(self):
        resp = await self._run(_kernel_result(), outcome=NoveltyOutcome(True, False))
        self.assertIn("novel", resp.metadata["understanding"]["phenomena"])

    async def test_unknown_novelty_is_not_flagged(self):
        resp = await self._run(_kernel_result(), outcome=NoveltyOutcome(None, False))
        self.assertNotIn("novel", resp.metadata["understanding"]["phenomena"])

    async def test_reply_unchanged_and_legacy_metadata_kept(self):
        resp = await self._run(_kernel_result())
        self.assertEqual(resp.reply, "৫০০ টাকা")
        self.assertEqual(resp.metadata["language"]["detected"], "mixed")
        self.assertIn("routing", resp.metadata)

    async def test_assembly_failure_never_breaks_reply(self):
        resp = await self._run(_kernel_result(), glb_raises=True)
        self.assertEqual(resp.reply, "৫০০ টাকা")
        self.assertIsNone(resp.metadata["understanding"])

    async def test_understanding_is_persisted_without_changing_reply(self):
        resp = await self._run(_kernel_result())
        self.persist.assert_awaited_once()
        kw = self.persist.await_args.kwargs
        self.assertEqual((kw["tenant_id"], kw["customer_id"], kw["conversation_id"], kw["message_id"]), (1, 2, 3, 4))
        self.assertEqual(kw["understanding"], resp.metadata["understanding"])
        self.assertIsNotNone(kw["experience_id"])

    async def test_nothing_persisted_when_assembly_failed(self):
        await self._run(_kernel_result(), glb_raises=True)
        self.persist.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
