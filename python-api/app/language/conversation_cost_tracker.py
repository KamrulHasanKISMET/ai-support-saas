"""
CONVERSATION COST TRACKER (Phase 4 — Scale/Optimize)

Tracks token spend and brain-saved LLM calls per conversation,
written to conversation_costs (db/init/014_phase4_scale_optimize.sql).

Called from core_agent.py after every Kernel run (non-blocking,
isolated). The key metric: intent_engine_calls_saved — incremented
every time intentSource='brain', meaning the LLM Intent Engine call
was skipped. This is the primary efficiency proof for Phase 4.

Never raises. A failure here never touches the customer reply.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger


class ConversationCostTracker:
    """
    Upserts one row per conversation into conversation_costs,
    accumulating token spend and brain-saved calls across all turns.
    Stateless singleton (conversation_cost_tracker below).
    """

    async def record(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        conversation_id: int,
        input_tokens: int | None,
        output_tokens: int | None,
        intent_source: str | None,  # 'brain' | 'llm' | None
        is_resolved: bool = False,
    ) -> None:
        """
        Upsert conversation_costs for this turn. Never raises.

        input_tokens / output_tokens: from KernelRunResponse. None means
            the LLM didn't report usage (e.g. fallback path) — treated
            as 0 for accumulation so the row still exists.

        intent_source: 'brain' → increment intent_engine_calls_saved.

        is_resolved: set True when state_engine marks conversation done.
            Flips resolved=TRUE and sets resolved_at=NOW().
        """
        try:
            await self._upsert(
                db,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                input_tokens=input_tokens or 0,
                output_tokens=output_tokens or 0,
                brain_saved=1 if intent_source == "brain" else 0,
                is_resolved=is_resolved,
            )
        except Exception:
            logger.error(
                "ConversationCostTracker.record failed tenant=%s conv=%s",
                tenant_id, conversation_id, exc_info=True,
            )

    async def _upsert(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        conversation_id: int,
        input_tokens: int,
        output_tokens: int,
        brain_saved: int,
        is_resolved: bool,
    ) -> None:
        await db.execute(
            text("""
                INSERT INTO conversation_costs (
                    tenant_id, conversation_id,
                    total_input_tokens, total_output_tokens,
                    turn_count, intent_engine_calls_saved,
                    resolved, last_turn_at, resolved_at
                ) VALUES (
                    :tenant_id, :conversation_id,
                    :input_tokens, :output_tokens,
                    1, :brain_saved,
                    :is_resolved, NOW(),
                    CASE WHEN :is_resolved THEN NOW() ELSE NULL END
                )
                ON CONFLICT (tenant_id, conversation_id) DO UPDATE
                    SET total_input_tokens        = conversation_costs.total_input_tokens
                                                  + EXCLUDED.total_input_tokens,
                        total_output_tokens       = conversation_costs.total_output_tokens
                                                  + EXCLUDED.total_output_tokens,
                        turn_count                = conversation_costs.turn_count + 1,
                        intent_engine_calls_saved = conversation_costs.intent_engine_calls_saved
                                                  + EXCLUDED.intent_engine_calls_saved,
                        resolved                  = CASE
                                                        WHEN :is_resolved THEN TRUE
                                                        ELSE conversation_costs.resolved
                                                    END,
                        resolved_at               = CASE
                                                        WHEN :is_resolved AND conversation_costs.resolved_at IS NULL
                                                        THEN NOW()
                                                        ELSE conversation_costs.resolved_at
                                                    END,
                        last_turn_at              = NOW()
            """),
            {
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "brain_saved": brain_saved,
                "is_resolved": is_resolved,
            },
        )
        await db.commit()


conversation_cost_tracker = ConversationCostTracker()
