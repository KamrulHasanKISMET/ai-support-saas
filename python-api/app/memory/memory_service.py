from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.ai_service import ai_service
from app.core.logging import logger

MEMORY_EXTRACTION_PROMPT = """Extract reusable facts about the customer from \
this message that would help future conversations (e.g. name, size, color \
preference, past purchases). Only extract facts explicitly stated or clearly \
implied — never guess.

Respond ONLY with JSON: {{"memories": [{{"key": "...", "value": "...", \
"confidence": 0.0-1.0}}]}}. If nothing worth remembering, return {{"memories": []}}.

Customer message: {message}
"""


class MemoryService:
    """
    Correct flow (architecture doc section 14):
        Customer Message -> Memory Extraction -> Structured Memory Proposal
        -> Validation -> Application Logic -> Database

    The LLM proposes; this service validates and is the only thing that
    writes to customer_memories. Memory always belongs to tenant_id +
    customer_id.
    """

    MIN_CONFIDENCE_TO_STORE = 0.55

    async def extract_and_store(
        self,
        db: AsyncSession,
        tenant_id: int,
        customer_id: int,
        message: str,
        *,
        usage_out: dict[str, int | None] | None = None,
    ) -> list[dict]:
        raw = await ai_service.complete_json(
            MEMORY_EXTRACTION_PROMPT.format(message=message),
            usage_out=usage_out,
        )
        proposals = raw.get("memories", []) or []

        stored: list[dict] = []
        for proposal in proposals:
            key = proposal.get("key")
            value = proposal.get("value")
            confidence = float(proposal.get("confidence", 0.0))

            if not key or not value:
                continue
            if confidence < self.MIN_CONFIDENCE_TO_STORE:
                logger.info(
                    "Discarding low-confidence memory proposal: %s=%s (%.2f)",
                    key,
                    value,
                    confidence,
                )
                continue

            await db.execute(
                text(
                    """
                    INSERT INTO customer_memories
                        (tenant_id, customer_id, memory_type, memory_key, memory_value, confidence)
                    VALUES
                        (:tenant_id, :customer_id, 'long_term', :key, :value, :confidence)
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "customer_id": customer_id,
                    "key": key,
                    "value": value,
                    "confidence": confidence,
                },
            )
            stored.append({"key": key, "value": value, "confidence": confidence})

        if stored:
            await db.commit()
        return stored


memory_service = MemoryService()
