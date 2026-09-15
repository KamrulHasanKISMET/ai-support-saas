from dataclasses import dataclass, field
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
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


class ContextEngine:
    """
    Question -> [Memory, RAG, Current Data] -> Unified Context -> LLM
    (architecture doc section 16).
    """

    async def assemble(
        self,
        db: AsyncSession,
        tenant_id: int,
        customer_id: int,
        question: str,
        state: StateSlots,
        retrieval_query: str | None = None,
    ) -> AssembledContext:
        """
        `question` is what gets shown to the LLM as "CUSTOMER QUESTION"
        (kept in the customer's own words). `retrieval_query` is what
        RAG actually searches with — pass the Language Engine's
        normalized_message here so retrieval works consistently
        regardless of Bangla/English/Banglish/mixed phrasing. Falls
        back to `question` if no normalized form is available.

        RAG and Memory are fetched independently on purpose: Memory
        lookup needs no embeddings and should never be sacrificed just
        because knowledge search fails (e.g. no embedding provider
        wired up yet — see app/ai/embedding_service.py). Previously a
        RAG failure raised out of this whole function before it could
        return, silently discarding memories that had already been
        fetched successfully. Now RAG failure only empties
        knowledge_chunks; memories are unaffected.
        """
        memory_started_at = time.perf_counter()
        memories = await memory_search.get_all(db, tenant_id, customer_id)
        memory_latency_ms = int((time.perf_counter() - memory_started_at) * 1000)

        search_query = retrieval_query or question
        rag_started_at = time.perf_counter()
        try:
            candidates = await hybrid_search.search(db, tenant_id, search_query)
            top_chunks = reranker.rerank(candidates)
        except Exception:
            logger.error(
                "RAG search failed for tenant=%s — continuing with memories "
                "but no knowledge chunks",
                tenant_id,
                exc_info=True,
            )
            top_chunks = []
        rag_latency_ms = int((time.perf_counter() - rag_started_at) * 1000)

        return AssembledContext(
            question=question,
            memories=memories,
            knowledge_chunks=top_chunks,
            state=state,
            memory_latency_ms=memory_latency_ms,
            rag_latency_ms=rag_latency_ms,
        )


context_engine = ContextEngine()
