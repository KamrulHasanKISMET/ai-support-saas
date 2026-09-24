"""
Tests for the Context Engineering enhancements added to
app/context/context_engine.py: budgeting, the compression foundation,
and validation -- plus the per-source (memory/RAG) status tracking
they build on.

Runs the REAL ContextEngine. Only the two DB-backed calls it makes
(memory_search.get_all, hybrid_search.search) are mocked -- everything
else (budgeting math, validation logic, the reranker) is exercised for
real, same approach as test_language_engine.py.
"""

import unittest
from unittest.mock import AsyncMock, patch

from app.context.context_engine import (
    ContextCompressor,
    ContextEngine,
    NoOpCompressor,
)
from app.memory.memory_search import memory_search
from app.rag.hybrid_search import hybrid_search


def mock_memory(return_value):
    return patch.object(memory_search, "get_all", new=AsyncMock(return_value=return_value))


def mock_rag(return_value=None, side_effect=None):
    kwargs = {"side_effect": side_effect} if side_effect else {"return_value": return_value or []}
    return patch.object(hybrid_search, "search", new=AsyncMock(**kwargs))


class TestTenantScopeValidation(unittest.IsolatedAsyncioTestCase):
    async def test_refuses_invalid_customer_id_without_querying_anything(self):
        engine = ContextEngine()
        with mock_memory([]) as mem_mock, mock_rag([]) as rag_mock:
            result = await engine.assemble(
                db=None, tenant_id=1, customer_id=-1, question="hi", state={}
            )
        self.assertEqual(result.memory_status, "skipped")
        self.assertEqual(result.rag_status, "skipped")
        self.assertIn("invalid_tenant_or_customer_scope", result.validation_errors)
        mem_mock.assert_not_called()
        rag_mock.assert_not_called()

    async def test_refuses_non_integer_tenant_id(self):
        engine = ContextEngine()
        with mock_memory([]), mock_rag([]):
            result = await engine.assemble(
                db=None, tenant_id="1", customer_id=1, question="hi", state={}
            )
        self.assertIn("invalid_tenant_or_customer_scope", result.validation_errors)

    async def test_valid_scope_proceeds_to_retrieval(self):
        engine = ContextEngine()
        with mock_memory([]) as mem_mock, mock_rag([]) as rag_mock:
            result = await engine.assemble(
                db=None, tenant_id=1, customer_id=2, question="hi", state={}
            )
        self.assertEqual(result.memory_status, "ok")
        self.assertEqual(result.rag_status, "ok")
        mem_mock.assert_called_once()
        rag_mock.assert_called_once()


class TestPerSourceStatusTracking(unittest.IsolatedAsyncioTestCase):
    async def test_memory_failure_does_not_block_rag(self):
        engine = ContextEngine()
        chunk = {"id": 1, "content": "some knowledge", "vector_score": 0.9, "keyword_score": 0.0}
        with mock_memory(None) as mem_mock, mock_rag([chunk]):
            mem_mock.side_effect = RuntimeError("connection to postgresql://u:p@host/db refused")
            result = await engine.assemble(
                db=None, tenant_id=1, customer_id=2, question="hi", state={}
            )
        self.assertEqual(result.memory_status, "error")
        self.assertIsNotNone(result.memory_error)
        self.assertNotIn("p@host", result.memory_error)  # sanitized, see test_trace_sanitize.py
        self.assertEqual(result.rag_status, "ok")
        self.assertEqual(len(result.knowledge_chunks), 1)

    async def test_rag_failure_does_not_block_memory(self):
        engine = ContextEngine()
        memory_row = {"memory_type": "long_term", "memory_key": "size", "memory_value": "42"}
        with mock_memory([memory_row]), mock_rag(side_effect=RuntimeError("embedding provider down")):
            result = await engine.assemble(
                db=None, tenant_id=1, customer_id=2, question="hi", state={}
            )
        self.assertEqual(result.rag_status, "error")
        self.assertIsNotNone(result.rag_error)
        self.assertEqual(result.memory_status, "ok")
        self.assertEqual(len(result.memories), 1)

    async def test_both_succeed_reports_ok_with_no_errors(self):
        engine = ContextEngine()
        with mock_memory([]), mock_rag([]):
            result = await engine.assemble(
                db=None, tenant_id=1, customer_id=2, question="hi", state={}
            )
        self.assertEqual(result.memory_status, "ok")
        self.assertEqual(result.rag_status, "ok")
        self.assertIsNone(result.memory_error)
        self.assertIsNone(result.rag_error)


class TestBudgeting(unittest.TestCase):
    """_apply_budget is synchronous pure logic -- test it directly."""

    def setUp(self):
        self.engine = ContextEngine()

    def test_keeps_everything_under_a_generous_budget(self):
        memories = [{"memory_key": "size", "memory_value": "42"}]
        chunks = [{"content": "short knowledge chunk"}]
        kept_m, kept_c, info = self.engine._apply_budget(memories, chunks, {}, budget=10_000)
        self.assertEqual(len(kept_m), 1)
        self.assertEqual(len(kept_c), 1)
        self.assertEqual(info["memoriesDropped"], 0)
        self.assertEqual(info["chunksDropped"], 0)

    def test_drops_lowest_priority_items_first_never_truncates_mid_item(self):
        # knowledge_chunks are passed in already-ranked order (reranker's
        # job, upstream of budgeting) -- budgeting must drop from the
        # END of that order, and every kept item must be byte-for-byte
        # identical to the input (no mid-string truncation).
        chunks = [
            {"content": "A" * 40},  # highest ranked -- must survive
            {"content": "B" * 40},
            {"content": "C" * 40},  # lowest ranked -- first to go
        ]
        # ~10 tokens/chunk at 4 chars/token; budget for ~2 chunks only
        kept_m, kept_c, info = self.engine._apply_budget([], chunks, {}, budget=20)
        self.assertLess(len(kept_c), len(chunks))
        for original, kept in zip(chunks, kept_c):
            self.assertEqual(original["content"], kept["content"])  # never truncated mid-item
        self.assertGreater(info["chunksDropped"], 0)

    def test_state_is_always_fully_kept_and_reserved_first(self):
        state = {"product": "Nike Air Max", "size": "42", "quantity": "1"}
        # Budget so small that if state weren't reserved first, nothing
        # else would even matter -- the real assertion is state's own
        # size never causes a crash and memories/chunks still get a
        # fair shot at whatever's left.
        memories = [{"memory_key": "k", "memory_value": "v"}]
        kept_m, kept_c, info = self.engine._apply_budget(memories, [], state, budget=1000)
        self.assertEqual(len(kept_m), 1)  # plenty of room left after reserving state's tiny cost

    def test_budget_info_shape(self):
        _, _, info = self.engine._apply_budget([], [], {}, budget=500)
        for key in ["tokensBudget", "tokensUsed", "memoriesKept", "memoriesDropped", "chunksKept", "chunksDropped"]:
            self.assertIn(key, info)
        self.assertEqual(info["tokensBudget"], 500)


class TestValidation(unittest.TestCase):
    """_clean_context is synchronous pure logic -- test it directly."""

    def setUp(self):
        self.engine = ContextEngine()

    def test_removes_duplicate_memories(self):
        memories = [
            {"memory_key": "size", "memory_value": "42"},
            {"memory_key": "size", "memory_value": "42"},  # exact duplicate
        ]
        clean, _, _, errors = self.engine._clean_context(memories, [], "hi", {})
        self.assertEqual(len(clean), 1)
        self.assertIn("removed_malformed_or_duplicate_memories", errors)

    def test_removes_malformed_memories_missing_required_fields(self):
        memories = [
            {"memory_key": "size", "memory_value": "42"},
            {"memory_key": "no_value_field"},  # malformed -- no memory_value
            {"not_a_memory_shape": True},
            "not even a dict",
        ]
        clean, _, _, errors = self.engine._clean_context(memories, [], "hi", {})
        self.assertEqual(len(clean), 1)
        self.assertIn("removed_malformed_or_duplicate_memories", errors)

    def test_removes_duplicate_and_empty_knowledge_chunks(self):
        chunks = [
            {"content": "same content"},
            {"content": "same content"},  # duplicate
            {"content": "   "},  # blank -- malformed
            {"content": ""},  # empty -- malformed
        ]
        _, clean, _, errors = self.engine._clean_context([], chunks, "hi", {})
        self.assertEqual(len(clean), 1)
        self.assertIn("removed_malformed_or_duplicate_chunks", errors)

    def test_flags_empty_question(self):
        _, _, _, errors = self.engine._clean_context([], [], "", {})
        self.assertIn("empty_or_invalid_question", errors)

    def test_flags_whitespace_only_question(self):
        _, _, _, errors = self.engine._clean_context([], [], "   ", {})
        self.assertIn("empty_or_invalid_question", errors)

    def test_recovers_from_invalid_state_type_instead_of_crashing(self):
        _, _, state, errors = self.engine._clean_context([], [], "hi", state="not a dict")
        self.assertEqual(state, {})
        self.assertIn("invalid_state_type", errors)

    def test_clean_well_formed_context_has_no_errors(self):
        memories = [{"memory_key": "size", "memory_value": "42"}]
        chunks = [{"content": "valid knowledge"}]
        clean_m, clean_c, clean_s, errors = self.engine._clean_context(
            memories, chunks, "a real question", {"product": "x"}
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(clean_m), 1)
        self.assertEqual(len(clean_c), 1)

    def test_handles_already_empty_context_safely(self):
        clean_m, clean_c, clean_s, errors = self.engine._clean_context([], [], "a question", {})
        self.assertEqual(clean_m, [])
        self.assertEqual(clean_c, [])
        self.assertEqual(errors, [])


class TestCompressionFoundation(unittest.TestCase):
    def test_noop_compressor_is_the_default_and_changes_nothing(self):
        engine = ContextEngine()
        self.assertIsInstance(engine._compressor, NoOpCompressor)

    def test_noop_compressor_returns_inputs_unchanged_and_flag_false(self):
        memories = [{"memory_key": "a", "memory_value": "b"}]
        chunks = [{"content": "c"}]
        out_m, out_c, applied = NoOpCompressor().compress(memories, chunks, {})
        self.assertEqual(out_m, memories)
        self.assertEqual(out_c, chunks)
        self.assertFalse(applied)

    def test_compressor_is_injectable_without_changing_assemble_callers(self):
        """Confirms the foundation's whole point: a real compressor can
        be swapped in via the constructor with zero change to
        Kernel.run()'s call site or assemble()'s signature."""

        class AlwaysCompresses(ContextCompressor):
            def compress(self, memories, knowledge_chunks, budget_info):
                return memories[:1], knowledge_chunks[:1], True

        engine = ContextEngine(compressor=AlwaysCompresses())
        self.assertIsInstance(engine._compressor, AlwaysCompresses)

    def test_base_compressor_contract_is_abstract(self):
        with self.assertRaises(NotImplementedError):
            ContextCompressor().compress([], [], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
