"""
NOVELTY DETECTOR (Phase 4 — Scale/Optimize)

docs/LANGUAGE_INTELLIGENCE.md Phase 4: flag messages that do not
resemble any known intent cluster (similarity below the tenant's
novelty_threshold) and log them to novelty_events for operator triage.

What this IS:
    A single async method, NoveltyDetector.check_and_log(), called from
    core_agent.py in the same non-blocking, isolated pattern as
    record_language_experience(). It receives the BrainPrediction
    already computed by ShadowBrain (so no second embedding call is
    needed) and compares its similarity against the tenant's configured
    novelty_threshold.

    A message is "novel" when:
      - brain_prediction is None (no clusters at all for this tenant), OR
      - brain_prediction.similarity < tenant_cfg.novelty_threshold

    Novel messages are written to novelty_events with the message text,
    the best similarity seen, the nearest intent, and the threshold used
    at log time. An operator (or a future triage UI) can then:
      - Confirm the message fits an existing intent (triage_result='existing_intent')
      - Identify a new intent the cluster set is missing (triage_result='new_intent')
      - Mark it as noise or spam

What this is NOT:
    - Not called from the hot Kernel path. Called from core_agent.py
      after the Kernel returns, in a try/except that never touches
      the reply.
    - Not a training signal by itself. Phase 3's ClusterBuilderService
      reads language_experiences (learning_eligible=TRUE); novelty
      events are a separate operator-facing signal.
    - Not the same as low similarity in RoutingService. RoutingService
      routes to 'llm' when similarity < calibrated_threshold (which
      could be 0.80). NoveltyDetector fires when similarity < novelty_threshold
      (default 0.50) -- a stricter "this message is genuinely unlike
      anything we know". The two thresholds are independent.

Isolation: check_and_log() wraps all DB work in try/except and never
raises. A novelty logging failure is not a customer-facing failure.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.language.shadow_brain import SHADOW_BRAIN_VERSION
from app.language.shadow_brain_types import BrainPrediction
from app.language.tenant_calibration_config import TenantCalibrationConfig


@dataclass(frozen=True)
class NoveltyOutcome:
    """is_novel and logged are DIFFERENT facts: a message can be novel
    while logging is disabled for the tenant (or the write failed).
    Callers that need the novelty signal itself (glb.TurnUnderstanding)
    must read is_novel, never infer it from `logged`. is_novel is None
    when it could not be determined (config load / any error)."""

    is_novel: bool | None
    logged: bool


class NoveltyDetector:
    """
    Checks BrainPrediction similarity against the tenant's novelty
    threshold and logs novel messages to novelty_events.

    Stateless singleton (novelty_detector below). All state in DB.
    """

    async def check_and_log(self, db: AsyncSession, **kwargs) -> bool:
        """Backward-compatible wrapper: True iff a novelty_events row was
        written. Never raises. Use check() when the novelty signal
        itself is needed. Same keyword arguments as check()."""
        return (await self.check(db, **kwargs)).logged

    async def check(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        customer_id: int,
        conversation_id: int,
        experience_id: uuid.UUID,
        request_id: str | None,
        normalized_message: str,
        brain_prediction: BrainPrediction | None,
    ) -> NoveltyOutcome:
        """
        Decide whether this message is novel and, if so and enabled,
        write a novelty_events row. Never raises.

        brain_prediction : ShadowBrain.predict() result for this turn;
            None means no clusters exist or embedding failed.
        normalized_message : the Language Engine's normalized form.
        """
        try:
            return await self._check(
                db,
                tenant_id=tenant_id,
                customer_id=customer_id,
                conversation_id=conversation_id,
                experience_id=experience_id,
                request_id=request_id,
                normalized_message=normalized_message,
                brain_prediction=brain_prediction,
            )
        except Exception:
            logger.error(
                "NoveltyDetector.check failed tenant=%s conv=%s",
                tenant_id, conversation_id, exc_info=True,
            )
            return NoveltyOutcome(is_novel=None, logged=False)

    async def _check(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        customer_id: int,
        conversation_id: int,
        experience_id: uuid.UUID,
        request_id: str | None,
        normalized_message: str,
        brain_prediction: BrainPrediction | None,
    ) -> NoveltyOutcome:
        # Load per-tenant config to get this tenant's novelty threshold.
        tenant_cfg = await TenantCalibrationConfig.load(db, tenant_id)

        # Determine if this message is novel.
        is_novel = False
        best_similarity: float | None = None
        best_intent: str | None = None

        if brain_prediction is None:
            # No prediction at all → no clusters → always novel.
            is_novel = True
        else:
            best_similarity = brain_prediction.similarity
            best_intent = brain_prediction.predicted_intent
            if best_similarity < tenant_cfg.novelty_threshold:
                is_novel = True

        if not is_novel:
            return NoveltyOutcome(is_novel=False, logged=False)

        if not tenant_cfg.novelty_logging_enabled:
            # Novel, but this tenant opted out of the operator log.
            return NoveltyOutcome(is_novel=True, logged=False)

        logger.info(
            "NoveltyDetector: novel message tenant=%s conv=%s "
            "similarity=%s threshold=%.2f nearest_intent=%s",
            tenant_id, conversation_id,
            f"{best_similarity:.3f}" if best_similarity is not None else "None",
            tenant_cfg.novelty_threshold,
            best_intent,
        )

        await db.execute(
            text("""
                INSERT INTO novelty_events (
                    tenant_id, customer_id, conversation_id,
                    experience_id, request_id,
                    normalized_message, best_similarity, best_intent,
                    novelty_threshold, brain_version
                ) VALUES (
                    :tenant_id, :customer_id, :conversation_id,
                    :experience_id, :request_id,
                    :normalized_message, :best_similarity, :best_intent,
                    :novelty_threshold, :brain_version
                )
            """),
            {
                "tenant_id": tenant_id,
                "customer_id": customer_id,
                "conversation_id": conversation_id,
                "experience_id": str(experience_id),
                "request_id": request_id,
                "normalized_message": normalized_message,
                "best_similarity": best_similarity,
                "best_intent": best_intent,
                "novelty_threshold": tenant_cfg.novelty_threshold,
                "brain_version": SHADOW_BRAIN_VERSION,
            },
        )
        await db.commit()
        return NoveltyOutcome(is_novel=True, logged=True)


novelty_detector = NoveltyDetector()
