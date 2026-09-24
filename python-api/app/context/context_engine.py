from dataclasses import dataclass, field
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.core.trace_sanitize import safe_error_message
from app.memory.memory_search import memory_search
from app.rag.context_builder import build_knowledge_context
from app.rag.hybrid_search import hybrid_search
from app.rag.reranker import reranker
from app.state.state_types import StateSlots


@dataclass
class AssembledContext:
    """Everything the LLM needs to answer — never the answer itself.
    The Context Engine does NOT answer the customer (section 16)."""

    question: str
    memories: list[dict] = field(default_factory=list)
    knowledge_chunks: list[dict] = field(default_factory=list)
    state: StateSlots = field(default_factory=dict)

    # Phase 1 observability (docs/OBSERVABILITY.md) — timing only, set
    # by assemble() below. None when assemble() itself wasn't called
    # (e.g. the Kernel's empty-context fallback on an unrelated error).
    memory_latency_ms: int | None = None
    rag_latency_ms: int | None = None

    # Commercial V1 trace lifecycle: per-source status, mirroring the
    # Kernel's own step-status convention ("ok" | "error" | "skipped").
    # A RAG/Memory failure still degrades gracefully (unchanged
    # behavior — see the try/except in assemble() below) but is no
    # longer INVISIBLE at the trace level.
    memory_status: str = "ok"
    memory_error: str | None = None
    rag_status: str = "ok"
    rag_error: str | None = None

    # Context Engineering additions (this phase) — see ContextEngine's
    # docstring below for what each stage does.
    budget_info: dict = field(default_factory=dict)
    compression_applied: bool = False
    validation_errors: list[str] = field(default_factory=list)

    def to_prompt_block(self) -> str:
        memory_lines = (
            "\n".join(f"- {m['memory_key']}: {m['memory_value']}" for m in self.memories)
            or "None known yet."
        )
        state_lines = (
            "\n".join(f"- {k}: {v}" for k, v in self.state.items()) or "None yet."
        )
        knowledge_block = build_knowledge_context(self.knowledge_chunks)

        return (
            f"CUSTOMER MEMORY:\n{memory_lines}\n\n"
            f"CONVERSATION STATE:\n{state_lines}\n\n"
            f"RELEVANT KNOWLEDGE:\n{knowledge_block}\n\n"
            f"CUSTOMER QUESTION:\n{self.question}"
        )


class ContextCompressor:
    """
    Foundation interface for future summarization/compaction — NOT
    implemented as a real compressor in this phase (task explicitly
    excludes "a complex autonomous summarization system"). Kept as an
    injectable strategy so a future implementation is a constructor
    argument swap in ContextEngine, not a change to assemble()'s
    control flow or call sites.

    Contract a future implementation MUST uphold: preserve critical
    facts, instructions, tenant/store identity, security context, and
    important tool results. The signature deliberately keeps memories/
    knowledge_chunks separate (rather than a single flattened blob) so
    a real compressor can choose to summarize knowledge_chunks only,
    for instance, while leaving customer-specific memories untouched.
    """

    def compress(
        self, memories: list[dict], knowledge_chunks: list[dict], budget_info: dict
    ) -> tuple[list[dict], list[dict], bool]:
        raise NotImplementedError


class NoOpCompressor(ContextCompressor):
    """Default, and the only implementation that exists today — does
    nothing. This IS current production behavior; nothing is
    compressed, so nothing critical can be lost by compression that
    doesn't happen."""

    def compress(self, memories, knowledge_chunks, budget_info):
        return memories, knowledge_chunks, False


class ContextEngine:
    """
    Question -> [Memory, RAG, Current Data] -> Unified Context -> LLM
    (architecture doc section 16).

    Commercial V1 Context Engineering pipeline (this phase — extends,
    does not replace, the engine above): retrieve -> filter/rank/select
    (existing hybrid search + reranker) -> budget -> optional
    compression -> validate -> build final context (to_prompt_block,
    unchanged). assemble()'s signature and Kernel.run()'s call site are
    UNCHANGED — every new step is additive internal structure, not a
    new interface.
    """

    def __init__(self, compressor: ContextCompressor | None = None):
        self._compressor = compressor or NoOpCompressor()

    def _validate_tenant_scope(self, tenant_id: int, customer_id: int) -> bool:
        """Fail-closed guard on the two ids every downstream query is
        scoped by. Every retrieval call below is already tenant/
        customer-filtered in its own SQL (memory_search, rag/search.py)
        — this is defense in depth at the boundary ContextEngine
        actually controls (its own inputs), refusing to even attempt
        retrieval with a missing/invalid scope rather than trusting a
        caller's id blindly (architecture doc section 3, tenant
        isolation)."""
        return (
            isinstance(tenant_id, int)
            and tenant_id > 0
            and isinstance(customer_id, int)
            and customer_id > 0
        )

    def _estimate_tokens(self, text: str) -> int:
        """Rough token estimate (~4 chars/token), not an exact
        tokenizer count — deliberately avoids adding a tokenizer
        dependency for a budget that only needs to be approximately
        right, not exact."""
        return max(1, len(text) // 4)

    def _apply_budget(
        self,
        memories: list[dict],
        knowledge_chunks: list[dict],
        state: StateSlots,
        budget: int,
    ) -> tuple[list[dict], list[dict], dict]:
        """
        Keeps items in their EXISTING priority order (memories: most
        recently updated first, per memory_search.get_all's own
        ORDER BY; knowledge_chunks: highest-relevance first, per
        reranker.rerank's own scoring) and drops whole low-priority
        items once the budget is exhausted — never truncates a single
        memory or chunk mid-string. State is always kept in full (it's
        small and required for slot-filling) and its estimated size is
        reserved from the budget first, not counted against
        memories/chunks priority.
        """
        state_tokens = self._estimate_tokens(str(state)) if state else 0
        remaining = max(0, budget - state_tokens)

        kept_memories: list[dict] = []
        for m in memories:
            cost = self._estimate_tokens(f"{m.get('memory_key', '')}: {m.get('memory_value', '')}")
            if cost > remaining:
                break
            kept_memories.append(m)
            remaining -= cost
        memories_dropped = len(memories) - len(kept_memories)

        kept_chunks: list[dict] = []
        for c in knowledge_chunks:
            cost = self._estimate_tokens(str(c.get("content", "")))
            if cost > remaining:
                break
            kept_chunks.append(c)
            remaining -= cost
        chunks_dropped = len(knowledge_chunks) - len(kept_chunks)

        budget_info = {
            "tokensBudget": budget,
            "tokensUsed": budget - remaining,
            "memoriesKept": len(kept_memories),
            "memoriesDropped": memories_dropped,
            "chunksKept": len(kept_chunks),
            "chunksDropped": chunks_dropped,
        }
        if memories_dropped or chunks_dropped:
            logger.info("Context budget trimmed context: %s", budget_info)
        return kept_memories, kept_chunks, budget_info

    def _clean_context(
        self,
        memories: list[dict],
        knowledge_chunks: list[dict],
        question: str,
        state: StateSlots,
    ) -> tuple[list[dict], list[dict], StateSlots, list[str]]:
        """
        Final validation gate: removes malformed/duplicate entries,
        confirms the required inputs (question, state) are well-formed,
        and safely handles an already-empty context (no-op on empty
        lists). Runs AFTER budgeting/compression so nothing either of
        those stages could introduce slips through unchecked.
        """
        errors: list[str] = []

        if not isinstance(question, str) or not question.strip():
            errors.append("empty_or_invalid_question")

        if not isinstance(state, dict):
            errors.append("invalid_state_type")
            state = {}

        clean_memories: list[dict] = []
        seen_memories: set[tuple] = set()
        for m in memories:
            if not isinstance(m, dict):
                continue
            key, value = m.get("memory_key"), m.get("memory_value")
            if not key or not value:
                continue
            dedup_key = (key, value)
            if dedup_key in seen_memories:
                continue
            seen_memories.add(dedup_key)
            clean_memories.append(m)
        if len(clean_memories) != len(memories):
            errors.append("removed_malformed_or_duplicate_memories")

        clean_chunks: list[dict] = []
        seen_chunks: set[str] = set()
        for c in knowledge_chunks:
            if not isinstance(c, dict):
                continue
            content = c.get("content")
            if not content or not str(content).strip():
                continue
            if content in seen_chunks:
                continue
            seen_chunks.add(content)
            clean_chunks.append(c)
        if len(clean_chunks) != len(knowledge_chunks):
            errors.append("removed_malformed_or_duplicate_chunks")

        return clean_memories, clean_chunks, state, errors

    async def assemble(
        self,
        db: AsyncSession,
        tenant_id: int,
        customer_id: int,
        question: str,
        state: StateSlots,
        retrieval_query: str | None = None,
        token_budget: int | None = None,
    ) -> AssembledContext:
        """
        `question` is what gets shown to the LLM as "CUSTOMER QUESTION"
        (kept in the customer's own words). `retrieval_query` is what
        RAG actually searches with — pass the Language Engine's
        normalized_message here so retrieval works consistently
        regardless of Bangla/English/Banglish/mixed phrasing. Falls
        back to `question` if no normalized form is available.
        `token_budget` is optional (defaults to
        settings.context_token_budget) — Kernel.run()'s call site does
        not need to pass it.

        RAG and Memory are fetched independently on purpose: Memory
        lookup needs no embeddings and should never be sacrificed just
        because knowledge search fails (e.g. no embedding provider
        wired up yet — see app/ai/embedding_service.py). A RAG or
        Memory failure degrades gracefully (empty results) exactly as
        before this phase — the only change is that the failure is now
        also reported via memory_status/rag_status instead of being
        silently swallowed after a log line.
        """
        budget = token_budget if token_budget is not None else settings.context_token_budget

        if not self._validate_tenant_scope(tenant_id, customer_id):
            logger.error(
                "Context assembly refused -- invalid tenant/customer scope tenant=%s customer=%s",
                tenant_id,
                customer_id,
            )
            return AssembledContext(
                question=question if isinstance(question, str) else "",
                state=state if isinstance(state, dict) else {},
                memory_status="skipped",
                rag_status="skipped",
                validation_errors=["invalid_tenant_or_customer_scope"],
            )

        # 1+2. RETRIEVE + FILTER/RANK/SELECT (unchanged logic; now with
        # per-source status/error tracking added around it)
        memory_status, memory_error = "ok", None
        memory_started_at = time.perf_counter()
        try:
            memories = await memory_search.get_all(db, tenant_id, customer_id)
        except Exception as exc:
            logger.error(
                "Memory retrieval failed for tenant=%s customer=%s",
                tenant_id,
                customer_id,
                exc_info=True,
            )
            memories = []
            memory_status, memory_error = "error", safe_error_message(exc)
        memory_latency_ms = int((time.perf_counter() - memory_started_at) * 1000)

        search_query = retrieval_query or question
        rag_status, rag_error = "ok", None
        rag_started_at = time.perf_counter()
        try:
            candidates = await hybrid_search.search(db, tenant_id, search_query)
            top_chunks = reranker.rerank(candidates)
        except Exception as exc:
            logger.error(
                "RAG search failed for tenant=%s — continuing with memories "
                "but no knowledge chunks",
                tenant_id,
                exc_info=True,
            )
            top_chunks = []
            rag_status, rag_error = "error", safe_error_message(exc)
        rag_latency_ms = int((time.perf_counter() - rag_started_at) * 1000)

        # 3. BUDGET
        budgeted_memories, budgeted_chunks, budget_info = self._apply_budget(
            memories, top_chunks, state, budget
        )

        # 4. OPTIONAL COMPRESSION (foundation only — NoOpCompressor by
        # default, see its docstring)
        compressed_memories, compressed_chunks, compression_applied = (
            self._compressor.compress(budgeted_memories, budgeted_chunks, budget_info)
        )

        # 5. VALIDATE
        clean_memories, clean_chunks, clean_state, validation_errors = self._clean_context(
            compressed_memories, compressed_chunks, question, state
        )

        return AssembledContext(
            question=question if isinstance(question, str) else "",
            memories=clean_memories,
            knowledge_chunks=clean_chunks,
            state=clean_state,
            memory_latency_ms=memory_latency_ms,
            rag_latency_ms=rag_latency_ms,
            memory_status=memory_status,
            memory_error=memory_error,
            rag_status=rag_status,
            rag_error=rag_error,
            budget_info=budget_info,
            compression_applied=compression_applied,
            validation_errors=validation_errors,
        )


context_engine = ContextEngine()
