"""
LANGUAGE VERIFICATION SERVICE — human confirmation / correction.

docs/GENERAL_LANGUAGE_BRAIN.md §2.2 (verification levels), §2.4
(disagreement / label revision / audit trail), §9 item 2.

Context: item 2 ("add verification_level, gate ClusterBuilderService on
it") shipped fully on the write/read side — the column exists
(db/init/015_language_verification.sql), core_agent.py already writes
'self_consistent' automatically on brain/LLM agreement, and
cluster_builder_service.py's `_fetch_eligible_rows()` already gates on
it. What was still missing: nothing anywhere ever wrote
'human_confirmed' or 'human_corrected' — the two levels that require an
actual person, not automatic agreement. This module is that missing
write path; docs/ROADMAP.md's not-yet-built "novelty-triage endpoint"
is one caller this makes possible, not the only one.

Two operations, both tenant-scoped. Both return a falsy value (False /
None) rather than raising when the row doesn't exist for this tenant,
so a route can turn that into a clean 404 instead of a 500 — this is
an operator correcting a specific row they're looking at, not a
best-effort background write, so "not found" is an ordinary outcome to
handle, not an error to isolate.

  confirm_experience() -- verification_level -> 'human_confirmed' on
      the row itself. In place: nothing is superseded, because nothing
      about the label changed, only its trust level.

  correct_experience() -- a human says the recorded final_intent was
      wrong. Per §2.4 the old label is never deleted or overwritten:
      a NEW language_experiences row is inserted carrying the corrected
      final_intent and verification_level='human_corrected', copying
      the rest of the original turn's fields (customer/conversation/
      channel/message text) so the new row is itself a complete,
      independently-learnable experience. The OLD row's superseded_by
      is then set to the new row's id — its own verification_level is
      left untouched (it was correct evidence of what was recorded and
      when; only its standing as the live label for this turn changes).
      cluster_builder_service.py's `superseded_by IS NULL` filter
      already excludes superseded rows from future learning; this is
      the first code path that ever makes that column non-NULL.

Deliberately NOT handled here (out of scope for this task):
  - Rejecting a *second* correction of an already-corrected row with a
    friendlier error than "not found" — today, correcting a row twice
    simply 404s the second time (superseded_by IS NULL excludes it),
    which is correct but terse. A UI can always correct the newest row
    in the chain instead.
  - Any authorization beyond the existing internal-secret dependency
    the route applies (see app/api/routes/verification.py) — there is
    no per-operator identity in this codebase yet to attribute a
    correction to (who made it), the same gap noted for role-based
    dashboard permissions in docs/ROADMAP.md §1.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.language.experience_service import insert_experience_row
from app.language.experience_types import LanguageExperience


async def confirm_experience(db: AsyncSession, tenant_id: int, experience_id: str) -> bool:
    """Mark a row as human-confirmed correct. Returns False if no
    matching, not-yet-superseded row exists for this tenant."""
    result = await db.execute(
        text(
            """
            UPDATE language_experiences
               SET verification_level = 'human_confirmed'
             WHERE experience_id = :experience_id
               AND tenant_id     = :tenant_id
               AND superseded_by IS NULL
            """
        ),
        {"experience_id": experience_id, "tenant_id": tenant_id},
    )
    await db.commit()
    return result.rowcount > 0


async def correct_experience(
    db: AsyncSession,
    tenant_id: int,
    experience_id: str,
    corrected_intent: str,
) -> str | None:
    """Record a human correction of a prior final_intent as a new,
    superseding row (§2.4). Returns the new row's experience_id, or
    None if no matching, not-yet-superseded row exists for this
    tenant (nothing is written)."""
    row = (
        await db.execute(
            text(
                """
                SELECT customer_id, conversation_id, message_id, request_id,
                       channel, original_message, detected_language,
                       reply_language, normalized_message, communication_style,
                       script, is_transliterated, transliterated_from, code_mixing,
                       language
                  FROM language_experiences
                 WHERE experience_id = :experience_id
                   AND tenant_id     = :tenant_id
                   AND superseded_by IS NULL
                """
            ),
            {"experience_id": experience_id, "tenant_id": tenant_id},
        )
    ).first()
    if row is None:
        return None

    new_experience = LanguageExperience(
        experience_id=uuid.uuid4(),
        tenant_id=tenant_id,
        customer_id=row.customer_id,
        conversation_id=row.conversation_id,
        message_id=row.message_id,
        request_id=row.request_id,
        channel=row.channel,
        original_message=row.original_message,
        detected_language=row.detected_language,
        reply_language=row.reply_language,
        normalized_message=row.normalized_message,
        communication_style=row.communication_style,
        final_intent=corrected_intent,
        verification_level="human_corrected",
        script=row.script,
        is_transliterated=row.is_transliterated,
        transliterated_from=row.transliterated_from,
        code_mixing=row.code_mixing,
        language=row.language,
    )
    # Unwrapped (not record_language_experience's isolated wrapper): an
    # explicit human correction should fail loudly if the write fails,
    # not silently no-op — see module docstring.
    await insert_experience_row(db, new_experience)

    await db.execute(
        text(
            """
            UPDATE language_experiences
               SET superseded_by = :new_id
             WHERE experience_id = :experience_id
               AND tenant_id     = :tenant_id
            """
        ),
        {
            "new_id": str(new_experience.experience_id),
            "experience_id": experience_id,
            "tenant_id": tenant_id,
        },
    )
    await db.commit()
    return str(new_experience.experience_id)
