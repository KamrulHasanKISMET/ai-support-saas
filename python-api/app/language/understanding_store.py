"""
TURN UNDERSTANDING STORE -- persistence for glb.TurnUnderstanding
(Phase 5, docs/PENDING_WORK.md C1). Table: db/init/018_turn_understandings.sql.

Write path follows every other hot-path write in this codebase: isolated
try/except, never raises, a logging failure must never affect the
customer-facing reply. Unlike some of them it also rolls the session
back on failure so a failed INSERT cannot poison the caller's next
statement.

RETENTION: 90 days (decided 2026-09-28). Enforced by purge_expired(),
run via `python -m app.language.understanding_retention`. Nothing
schedules it yet (PENDING_WORK.md C1a).

Stores metadata only -- no message text (§5.3, see the migration).
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger

# Must equal the INTERVAL in db/init/018_turn_understandings.sql
# (pinned by tests/test_understanding_store.py).
TURN_UNDERSTANDING_RETENTION_DAYS = 90
PURGE_BATCH_SIZE = 5000

_INSERT_KEYS = (
    "language", "script", "is_transliterated", "code_mixing", "reply_language",
    "intent", "intent_confidence", "intent_source",
    "language_learning_eligible", "automation_eligible",
    "promotion_eligible", "training_eligible", "phenomena",
)


# Advisory plan (P5-4a, migration 026). Nullable: a turn whose plan failed,
# or a row written before 026, simply has NULLs.
_PLAN_KEYS = (
    "plan_understanding_source", "plan_automation", "plan_clarify",
    "plan_triage", "plan_record_as_learning", "plan_reasons",
)


def plan_to_params(plan: dict | None) -> dict | None:
    """Map glb_orchestrator.plan_to_dict() output to the plan_* columns.
    Returns None when there is no plan. Only fixed, non-text fields are
    copied (`enacted`/`advisory` are constants and are not stored)."""
    if not plan:
        return None
    return {
        "plan_understanding_source": plan.get("understanding_source"),
        "plan_automation": plan.get("automation"),
        "plan_clarify": plan.get("clarify_before_acting"),
        "plan_triage": plan.get("flag_for_triage"),
        "plan_record_as_learning": plan.get("record_as_learning_material"),
        "plan_reasons": list(plan.get("reasons") or []),
    }


async def insert_turn_understanding(
    db: AsyncSession,
    *,
    tenant_id: int,
    customer_id: int,
    conversation_id: int,
    message_id: int | None,
    request_id: str | None,
    experience_id: uuid.UUID | None,
    understanding: dict,
    plan: dict | None = None,
) -> None:
    """Bare INSERT (no isolation). `understanding` is
    glb.understanding_to_dict() output; normalized_message in it is
    deliberately NOT persisted. `plan` (optional) is
    glb_orchestrator.plan_to_dict() output; requires migration 026."""
    plan_params = plan_to_params(plan)
    params = {k: understanding.get(k) for k in _INSERT_KEYS}
    params["phenomena"] = list(understanding.get("phenomena") or [])
    params.update(
        tenant_id=tenant_id,
        customer_id=customer_id,
        conversation_id=conversation_id,
        message_id=message_id,
        request_id=request_id,
        experience_id=str(experience_id) if experience_id else None,
        retention_days=TURN_UNDERSTANDING_RETENTION_DAYS,
    )
    plan_cols = plan_vals = ""
    if plan_params is not None:
        params.update(plan_params)
        plan_cols = ", " + ", ".join(_PLAN_KEYS)
        plan_vals = ", " + ", ".join(":" + k for k in _PLAN_KEYS)
    await db.execute(
        text(
            f"""
            INSERT INTO turn_understandings (
                tenant_id, customer_id, conversation_id, message_id,
                request_id, experience_id,
                language, script, is_transliterated, code_mixing, reply_language,
                intent, intent_confidence, intent_source,
                language_learning_eligible, automation_eligible,
                promotion_eligible, training_eligible, phenomena,
                expires_at{plan_cols}
            ) VALUES (
                :tenant_id, :customer_id, :conversation_id, :message_id,
                :request_id, :experience_id,
                COALESCE(:language, 'und'), COALESCE(:script, 'und'),
                COALESCE(:is_transliterated, FALSE), COALESCE(:code_mixing, 'none'),
                :reply_language,
                :intent, :intent_confidence, :intent_source,
                COALESCE(:language_learning_eligible, TRUE),
                COALESCE(:automation_eligible, TRUE),
                COALESCE(:promotion_eligible, TRUE),
                COALESCE(:training_eligible, TRUE),
                :phenomena,
                CURRENT_TIMESTAMP + make_interval(days => :retention_days){plan_vals}
            )
            """
        ),
        params,
    )
    await db.commit()


async def record_turn_understanding(db: AsyncSession, **kwargs) -> bool:
    """Isolated write. True if persisted, False otherwise. Never raises.

    If the write fails while a `plan` was supplied (typically: migration
    026 not applied yet), it is retried ONCE without the plan so that
    deploying code before the migration cannot silently stop recording
    the understanding itself."""
    if not kwargs.get("understanding"):
        return False
    try:
        await insert_turn_understanding(db, **kwargs)
        return True
    except Exception:
        logger.error(
            "Failed to record turn_understanding tenant=%s conv=%s",
            kwargs.get("tenant_id"), kwargs.get("conversation_id"), exc_info=True,
        )
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001 -- rollback is best-effort
            pass
    if kwargs.get("plan"):
        try:
            retry = {**kwargs, "plan": None}
            await insert_turn_understanding(db, **retry)
            logger.warning(
                "turn_understanding recorded WITHOUT plan (is migration 026 applied?) "
                "tenant=%s conv=%s", kwargs.get("tenant_id"), kwargs.get("conversation_id"),
            )
            return True
        except Exception:
            logger.error("Retry without plan also failed", exc_info=True)
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
    return False


async def purge_expired(db: AsyncSession, *, batch_size: int = PURGE_BATCH_SIZE,
                        max_batches: int = 1000, dry_run: bool = False) -> int:
    """Delete rows past expires_at, in batches so one purge never holds
    a long lock. Returns rows deleted (or, with dry_run, rows that WOULD
    be deleted). Raises on DB error: a purge is an operator/cron job and
    must fail loudly, unlike the hot-path write."""
    if dry_run:
        row = (await db.execute(
            text("SELECT COUNT(*) AS n FROM turn_understandings WHERE expires_at < CURRENT_TIMESTAMP")
        )).first()
        return int(row.n or 0)

    total = 0
    for _ in range(max_batches):
        result = await db.execute(
            text(
                """
                DELETE FROM turn_understandings
                 WHERE id IN (
                     SELECT id FROM turn_understandings
                      WHERE expires_at < CURRENT_TIMESTAMP
                      ORDER BY id
                      LIMIT :batch
                 )
                """
            ),
            {"batch": batch_size},
        )
        await db.commit()
        n = result.rowcount or 0
        total += n
        if n < batch_size:
            break
    return total
