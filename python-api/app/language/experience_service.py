"""
LANGUAGE EXPERIENCE STORE — write path.

Responsibility: persist one LanguageExperience row per Language Engine
call. Phase 0 of the Language Intelligence design doc: "define the
schema, start logging" -- no own Brain, no confidence calibration, no
routing, no learning pipeline yet. Those are later phases and are
NOT started by this file.

Where this belongs: called from app/agent/core_agent.py, right after
record_trace() -- the same "one full Agent run just finished" boundary,
for the same reason trace_service.py lives there rather than inside
the Kernel itself (Kernel doesn't know about agent_run_id/request-level
concerns).

Error handling: isolated try/except around the INSERT + commit,
returning normally either way -- a write failure here must NEVER block
or alter the customer-facing reply, exactly the same isolation pattern
already used for Agent Run Trace and for Memory extraction in
app/kernel/kernel.py.

Tenant isolation: every row requires tenant_id (NOT NULL, FK'd) --
enforced by the table schema, not just application discipline. Every
future read/query path over this table must filter by tenant_id the
same way every other tenant-scoped engine in this codebase already
does (see AI_CONTEXT.md).
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.language.experience_types import LanguageExperience


async def insert_experience_row(db: AsyncSession, experience: LanguageExperience) -> None:
    """The bare INSERT, with no error isolation. Used by two callers with
    different failure semantics: record_language_experience() below wraps
    this in the isolated try/except every hot-path write in this codebase
    uses (a logging failure must never affect the customer-facing reply);
    app/language/verification_service.py calls this directly, unwrapped,
    because an explicit human-triggered correction should fail loudly
    (surface as a 500 to the caller) rather than silently no-op."""
    await db.execute(
        text(
            """
            INSERT INTO language_experiences (
                experience_id, tenant_id, customer_id, conversation_id,
                message_id, request_id, channel,
                original_message, detected_language, reply_language,
                normalized_message, communication_style,
                is_ambiguous, ambiguity_reason, entity_spans,
                brain_used, brain_prediction, brain_version,
                llm_called, llm_raw_confidence, llm_model_version,
                final_intent, verification_result, learning_eligible,
                verification_level, source_reliability, superseded_by,
                script, is_transliterated, transliterated_from, code_mixing,
                language
            ) VALUES (
                :experience_id, :tenant_id, :customer_id, :conversation_id,
                :message_id, :request_id, :channel,
                :original_message, :detected_language, :reply_language,
                :normalized_message, :communication_style,
                :is_ambiguous, :ambiguity_reason, :entity_spans,
                :brain_used, :brain_prediction, :brain_version,
                :llm_called, :llm_raw_confidence, :llm_model_version,
                :final_intent, :verification_result, :learning_eligible,
                :verification_level, :source_reliability, :superseded_by,
                :script, :is_transliterated, :transliterated_from, :code_mixing,
                :language
            )
            """
        ),
        {
            "experience_id": str(experience.experience_id),
            "tenant_id": experience.tenant_id,
            "customer_id": experience.customer_id,
            "conversation_id": experience.conversation_id,
            "message_id": experience.message_id,
            "request_id": experience.request_id,
            "channel": experience.channel,
            "original_message": experience.original_message,
            "detected_language": experience.detected_language,
            "reply_language": experience.reply_language,
            "normalized_message": experience.normalized_message,
            "communication_style": experience.communication_style,
            "is_ambiguous": experience.is_ambiguous,
            "ambiguity_reason": experience.ambiguity_reason,
            "entity_spans": experience.entity_spans,
            "brain_used": experience.brain_used,
            "brain_prediction": experience.brain_prediction,
            "brain_version": experience.brain_version,
            "llm_called": experience.llm_called,
            "llm_raw_confidence": experience.llm_raw_confidence,
            "llm_model_version": experience.llm_model_version,
            "final_intent": experience.final_intent,
            "verification_result": experience.verification_result,
            "learning_eligible": experience.learning_eligible,
            "verification_level": experience.verification_level,
            "source_reliability": experience.source_reliability,
            "superseded_by": experience.superseded_by,
            "script": experience.script,
            "is_transliterated": experience.is_transliterated,
            "transliterated_from": experience.transliterated_from,
            "code_mixing": experience.code_mixing,
            "language": experience.language,
        },
    )
    await db.commit()


async def record_language_experience(db: AsyncSession, experience: LanguageExperience) -> None:
    try:
        await insert_experience_row(db, experience)
    except Exception:
        logger.error(
            "Failed to record language_experience experience_id=%s tenant=%s conv=%s",
            experience.experience_id,
            experience.tenant_id,
            experience.conversation_id,
            exc_info=True,
        )

