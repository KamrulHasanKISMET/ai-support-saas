from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.intent.intent_types import IntentResult
from app.state.state_types import StateSlots


class StateEngine:
    """
    Tracks "what is the current condition of this conversation?"
    (architecture doc section 19/20). Customers should not need to
    repeat information already established earlier in the conversation.

    State transitions are explicit: this engine only writes slots that
    the Intent Engine extracted as entities for the current turn, then
    merges them with whatever slots already existed.
    """

    async def get_state(
        self, db: AsyncSession, tenant_id: int, conversation_id: int
    ) -> StateSlots:
        rows = await db.execute(
            text(
                """
                SELECT state_key, state_value
                  FROM conversation_states
                 WHERE tenant_id = :tenant_id AND conversation_id = :conversation_id
                """
            ),
            {"tenant_id": tenant_id, "conversation_id": conversation_id},
        )
        return {row.state_key: row.state_value for row in rows}

    async def update_state(
        self,
        db: AsyncSession,
        tenant_id: int,
        conversation_id: int,
        intent_result: IntentResult,
    ) -> StateSlots:
        for key, value in intent_result.entities.items():
            await db.execute(
                text(
                    """
                    INSERT INTO conversation_states
                        (tenant_id, conversation_id, state_key, state_value, confidence)
                    VALUES
                        (:tenant_id, :conversation_id, :state_key, :state_value, :confidence)
                    ON CONFLICT (conversation_id, state_key)
                    DO UPDATE SET state_value = EXCLUDED.state_value,
                                  confidence = EXCLUDED.confidence,
                                  updated_at = CURRENT_TIMESTAMP
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "conversation_id": conversation_id,
                    "state_key": key,
                    "state_value": str(value),
                    "confidence": intent_result.confidence,
                },
            )
        await db.commit()
        return await self.get_state(db, tenant_id, conversation_id)


state_engine = StateEngine()
