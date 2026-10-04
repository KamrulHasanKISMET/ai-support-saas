"""
SHADOW (PASSIVE) LANGUAGE BRAIN -- Phase 1 of the Language Intelligence
design doc (docs/LANGUAGE_INTELLIGENCE.md -- read that file before
touching this one).

Goal of this phase, verbatim from the design doc: introduce the first
version of the own Language Brain, running IN SHADOW ONLY -- it must
not affect any customer-facing response yet.

What this file IS: a minimal nearest-example intent matcher. Given a
normalized message, it embeds it (reusing the existing
app/ai/embedding_service.py provider abstraction -- no new vendor is
hardwired in here) and returns the closest `intent_clusters` row
(db/init/011_intent_clusters.sql) for that tenant, or None. That is
the entire v1 own Brain -- no fine-tuning, no custom model training.

What this file is NOT and must never become without a deliberate,
reviewed change:
    - It is never called from app/kernel/kernel.py. It is only ever
      called from app/agent/core_agent.py, strictly AFTER the Kernel
      has already produced its real, LLM-driven `reply`/`intent` for
      the turn -- so nothing in this file, including a failure in it,
      can ever change what the customer receives.
    - It never raises out of predict() -- every failure mode (no
      clusters yet, embedding provider unavailable, a DB error) is
      caught and turned into a plain `None` ("no shadow prediction
      this turn"), the same isolation pattern already used by
      record_trace()/record_language_experience().
    - It does not decide live routing. Comparing its predictions
      against the LLM's real intent (agreement/disagreement) is Phase
      1's whole point -- see app/agent/core_agent.py, which logs that
      comparison and stores it in `language_experiences.brain_prediction`.
      Actually ROUTING traffic to this brain is Phase 2, not started.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.embedding_service import embedding_service
from app.core.logging import logger
from app.intent.intent_types import IntentType
from app.language.shadow_brain_types import BrainPrediction

# Bumped whenever the matching approach itself changes (embedding
# model/provider, similarity metric, default seed set) -- stored on
# every prediction (language_experiences.brain_version) so later
# phases can tell which version of the brain produced which row.
SHADOW_BRAIN_VERSION = "shadow-v1-nearest-example"

# Minimal bootstrap set: one or two canonical examples per intent, used
# ONLY so the matcher has something to compare against before any real
# per-tenant ingestion exists. Deliberately NOT a curated dataset or a
# trained model -- Phase 1's explicit scope is "no fine-tuning, no
# custom model training yet". A real cluster-building pipeline sourced
# from this tenant's own `language_experiences` rows is Phase 3
# (automated learning/promotion), not this file.
_DEFAULT_CLUSTER_EXAMPLES: dict[IntentType, list[str]] = {
    IntentType.ORDER_STATUS: [
        "আমার অর্ডারটা কোথায়, কবে পাবো?",
        "where is my order, has it shipped yet",
    ],
    IntentType.PRODUCT_INFO: [
        "এই প্রোডাক্টটা সম্পর্কে বিস্তারিত বলুন",
        "can you tell me more details about this product",
    ],
    IntentType.PRODUCT_AVAILABILITY: [
        "এইটা কি স্টকে আছে?",
        "is this item currently in stock",
    ],
    IntentType.PRICE_INQUIRY: [
        "এইটার দাম কত?",
        "how much does this cost",
    ],
    IntentType.DELIVERY_INFO: [
        "ডেলিভারি চার্জ কত এবং কতদিন লাগবে?",
        "how much is delivery and how long does it take",
    ],
    IntentType.RETURN_REQUEST: [
        "আমি এই প্রোডাক্টটা ফেরত দিতে চাই",
        "i would like to return this item",
    ],
    IntentType.COMPLAINT: [
        "প্রোডাক্টটা নষ্ট অবস্থায় পেয়েছি, এটা নিয়ে অভিযোগ করছি",
        "i am very unhappy with the service, this is a complaint",
    ],
    IntentType.NEGOTIATION: [
        "একটু কম দামে হবে না?",
        "can you come down a bit on the price",
    ],
    IntentType.CREATE_ORDER: [
        "আমি এইটা অর্ডার করতে চাই",
        "i want to place an order for this",
    ],
    IntentType.GENERAL_QUESTION: [
        "হ্যালো, একটা প্রশ্ন ছিল",
        "hi, i had a general question",
    ],
}


@dataclass
class _FlatExample:
    intent: IntentType
    text: str


class ShadowBrain:
    """
    v1 own Language Brain -- nearest-example intent matcher, running in
    SHADOW ONLY. See this module's docstring for the isolation
    guarantees a caller can rely on.
    """

    async def predict(
        self, db: AsyncSession, tenant_id: int, normalized_message: str
    ) -> BrainPrediction | None:
        """
        Returns the closest known intent cluster for this tenant, or
        None if nothing could be predicted -- no clusters yet, the
        embedding provider is unavailable, or any other failure. None
        is always a safe, honest "no shadow prediction this turn",
        never fabricated.
        """
        if not normalized_message or not normalized_message.strip():
            return None

        try:
            await self._ensure_seeded(db, tenant_id)

            query_embedding = await embedding_service.embed(normalized_message)

            result = await db.execute(
                text(
                    """
                    SELECT id, intent, example_message,
                           1 - (embedding <=> :query_embedding) AS similarity
                      FROM intent_clusters
                     WHERE tenant_id = :tenant_id
                     ORDER BY embedding <=> :query_embedding
                     LIMIT 1
                    """
                ),
                {"tenant_id": tenant_id, "query_embedding": str(query_embedding)},
            )
            row = result.first()
            if row is None:
                return None

            return BrainPrediction(
                predicted_intent=row.intent,
                similarity=float(row.similarity),
                cluster_id=row.id,
                matched_example=row.example_message,
            )
        except Exception:
            logger.error(
                "Shadow brain prediction failed tenant=%s -- shadow mode only, "
                "this never affects the reply the customer already received",
                tenant_id,
                exc_info=True,
            )
            return None

    async def _ensure_seeded(self, db: AsyncSession, tenant_id: int) -> None:
        """
        Idempotent per-tenant bootstrap: if this tenant already has any
        `intent_clusters` rows, does nothing. Otherwise inserts the
        default example set (one batched embedding call, then one
        INSERT per example, tagged source='default_seed') so the
        matcher has something real to compare against.

        This is a stopgap, not the design's real cluster-building
        pipeline (that pipeline would build clusters from a tenant's
        own accumulated `language_experiences` -- Phase 3, not
        started). It only ever runs once per tenant, on that tenant's
        first turn after this deploy.
        """
        count_result = await db.execute(
            text("SELECT COUNT(*) AS n FROM intent_clusters WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id},
        )
        row = count_result.first()
        if row is not None and row.n > 0:
            return

        flat_examples = [
            _FlatExample(intent=intent, text=example)
            for intent, examples in _DEFAULT_CLUSTER_EXAMPLES.items()
            for example in examples
        ]
        embeddings = await embedding_service.embed_batch([fe.text for fe in flat_examples])

        for flat_example, embedding in zip(flat_examples, embeddings):
            await db.execute(
                text(
                    """
                    INSERT INTO intent_clusters
                        (tenant_id, intent, example_message, embedding, source)
                    VALUES
                        (:tenant_id, :intent, :example_message, :embedding, 'default_seed')
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "intent": flat_example.intent.value,
                    "example_message": flat_example.text,
                    "embedding": str(embedding),
                },
            )
        await db.commit()


shadow_brain = ShadowBrain()
