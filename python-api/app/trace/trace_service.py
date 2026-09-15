"""
AGENT RUN TRACE — write path.

Responsibility: persist one AgentRunTrace row per CoreAgent.run() call.
This is a WRITE-ONLY foundation -- "do not build a complete analytics
platform yet" (task constraint): no query/aggregation helpers, no read
API/route. Query `agent_run_traces` directly with SQL for now.

Where this belongs: called from app/agent/core_agent.py, the one place
that already sits at the "one full Agent run" boundary (Kernel itself
doesn't know about agent_run_id or wall-clock latency measured from
the outside). Kept in its own file rather than inlined in CoreAgent,
matching the existing pattern (agent_config.py, memory_service.py,
state_engine.py all keep their SQL in dedicated files, not in the
engines/classes that call them).

Error handling: isolated try/except around the INSERT + commit,
returning normally either way. A trace-write failure must NEVER block
or alter the customer-facing reply -- exactly the same isolation
pattern already used for Memory extraction in app/kernel/kernel.py.
CoreAgent.run() calls this AFTER building the AgentResponse it's about
to return, so there's no path where a trace failure delays or breaks
the actual answer.

Tenant isolation: every row requires tenant_id (NOT NULL, FK'd) --
enforced by the table schema, not just application discipline.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.trace.trace_types import AgentRunTrace


async def record_trace(db: AsyncSession, trace: AgentRunTrace) -> None:
    try:
        await db.execute(
            text(
                """
                INSERT INTO agent_run_traces (
                    agent_run_id, request_id, tenant_id, customer_id,
                    conversation_id, message_id,
                    detected_language, reply_language, normalized_input,
                    intent, confidence, state,
                    retrieval_used, retrieval_chunk_count,
                    decision, tools_called,
                    response, model,
                    latency_ms, cost_usd, error,
                    language_latency_ms, intent_latency_ms, context_latency_ms,
                    memory_latency_ms, rag_latency_ms, llm_latency_ms,
                    error_category
                ) VALUES (
                    :agent_run_id, :request_id, :tenant_id, :customer_id,
                    :conversation_id, :message_id,
                    :detected_language, :reply_language, :normalized_input,
                    :intent, :confidence, :state,
                    :retrieval_used, :retrieval_chunk_count,
                    :decision, :tools_called,
                    :response, :model,
                    :latency_ms, :cost_usd, :error,
                    :language_latency_ms, :intent_latency_ms, :context_latency_ms,
                    :memory_latency_ms, :rag_latency_ms, :llm_latency_ms,
                    :error_category
                )
                """
            ),
            {
                "agent_run_id": str(trace.agent_run_id),
                "request_id": trace.request_id,
                "tenant_id": trace.tenant_id,
                "customer_id": trace.customer_id,
                "conversation_id": trace.conversation_id,
                "message_id": trace.message_id,
                "detected_language": trace.detected_language,
                "reply_language": trace.reply_language,
                "normalized_input": trace.normalized_input,
                "intent": trace.intent,
                "confidence": trace.confidence,
                "state": trace.state,
                "retrieval_used": trace.retrieval_used,
                "retrieval_chunk_count": trace.retrieval_chunk_count,
                "decision": trace.decision,
                "tools_called": trace.tools_called,
                "response": trace.response,
                "model": trace.model,
                "latency_ms": trace.latency_ms,
                "cost_usd": trace.cost_usd,
                "error": trace.error,
                "language_latency_ms": trace.language_latency_ms,
                "intent_latency_ms": trace.intent_latency_ms,
                "context_latency_ms": trace.context_latency_ms,
                "memory_latency_ms": trace.memory_latency_ms,
                "rag_latency_ms": trace.rag_latency_ms,
                "llm_latency_ms": trace.llm_latency_ms,
                "error_category": trace.error_category,
            },
        )
        await db.commit()
    except Exception:
        logger.error(
            "Failed to record agent_run_trace agent_run_id=%s tenant=%s conv=%s",
            trace.agent_run_id,
            trace.tenant_id,
            trace.conversation_id,
            exc_info=True,
        )
