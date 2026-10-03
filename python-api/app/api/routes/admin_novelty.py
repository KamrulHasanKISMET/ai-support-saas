"""
Admin: novelty event triage (Phase 4 completion, step 2).

    GET   /admin/tenants/{tenant_id}/novelty-events?status=&limit=&offset=
    PATCH /admin/novelty-events/{event_id}?tenantId=

`novelty_events` (migration 014) already has the triage columns
(triaged, triage_result, triage_intent, triaged_at, triaged_by); nothing
could set them until now.

What triage DOES and does NOT do (important -- do not over-read it):
    It records an operator's judgement on a message the Brain found
    unlike any known cluster: 'new_intent' (a gap in the intent set),
    'existing_intent' (the Brain missed something that fits an existing
    intent), 'noise', or 'spam'. It only writes those columns. NOTHING
    reads them yet -- ClusterBuilderService still learns only from
    language_experiences, and a 'new_intent' verdict does not create an
    intent. The value today is the operator's audit trail and a clean
    untriaged queue. Wiring triage into learning is a separate,
    deliberate design step (it would touch what the Brain learns).

Protection model: same as the other admin routes -- internal secret,
no dashboard role layer yet (docs/ROADMAP.md §1). PATCH additionally
REQUIRES ?tenantId= and only updates a row belonging to that tenant, so
a wrong id can never edit another tenant's event; the row is simply
"not found" for that tenant (404).

`normalized_message` is customer text; these responses are for
operators only. `triagedBy` is a caller-supplied label (there is no
operator identity to derive it from yet) -- treat it as an audit note,
not authenticated identity.

Validation is plain Python (validate_triage / validate_paging) so it is
unit-tested without FastAPI or a database.
"""

import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.logging import logger
from app.core.security import require_internal_secret
from app.intent.intent_types import IntentType

router = APIRouter(
    prefix="/admin",
    tags=["admin-novelty"],
    dependencies=[Depends(require_internal_secret)],
)

# Matches the comment on novelty_events.triage_result (migration 014).
TRIAGE_RESULTS = ("new_intent", "existing_intent", "noise", "spam")
EXISTING_INTENTS = frozenset(i.value for i in IntentType)

STATUSES = ("untriaged", "triaged", "all")
# Fixed SQL fragments keyed by the validated status -- user input is
# never interpolated into SQL.
_STATUS_CLAUSE = {
    "untriaged": "AND triaged = FALSE",
    "triaged": "AND triaged = TRUE",
    "all": "",
}

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
MAX_TRIAGED_BY_LEN = 100
_NEW_INTENT_LABEL = re.compile(r"^[A-Z][A-Z0-9_]{2,99}$")  # triage_intent is VARCHAR(100)

_COLUMNS = """
    id, conversation_id, experience_id, request_id, normalized_message,
    best_similarity, best_intent, novelty_threshold, brain_version,
    triaged, triage_result, triage_intent, triaged_at, triaged_by, created_at
"""


class TriageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    triageResult: str
    triageIntent: str | None = None
    triagedBy: str


# ── Pure validation ──────────────────────────────────────────────────

def validate_triage(body: dict) -> tuple[dict | None, list[str]]:
    """
    body: {"triageResult", "triageIntent", "triagedBy"} (values may be None).
    Returns (clean, errors); clean is None when errors is non-empty.

    Rules
      existing_intent : triageIntent REQUIRED and must be a known IntentType.
      new_intent      : triageIntent optional, a proposed UPPER_SNAKE label;
                        must not equal an existing intent (that would be an
                        'existing_intent' verdict).
      noise / spam    : triageIntent must be absent.
    """
    errors: list[str] = []
    result = body.get("triageResult")
    intent = body.get("triageIntent")
    by = body.get("triagedBy")

    if isinstance(intent, str):
        intent = intent.strip() or None
    if isinstance(by, str):
        by = by.strip()

    if result not in TRIAGE_RESULTS:
        errors.append(f"triageResult must be one of {', '.join(TRIAGE_RESULTS)}")
    if not by:
        errors.append("triagedBy is required")
    elif len(by) > MAX_TRIAGED_BY_LEN:
        errors.append(f"triagedBy must be at most {MAX_TRIAGED_BY_LEN} characters")

    if result == "existing_intent":
        if intent is None:
            errors.append("triageIntent is required when triageResult is existing_intent")
        elif intent not in EXISTING_INTENTS:
            errors.append(
                f"triageIntent must be one of the existing intents: "
                f"{', '.join(sorted(EXISTING_INTENTS))}"
            )
    elif result == "new_intent":
        if intent is not None:
            if intent in EXISTING_INTENTS:
                errors.append(
                    f"{intent} already exists; use triageResult existing_intent instead"
                )
            elif not _NEW_INTENT_LABEL.match(intent):
                errors.append(
                    "triageIntent for new_intent must be an UPPER_SNAKE_CASE label "
                    "(3-100 characters)"
                )
    elif result in ("noise", "spam"):
        if intent is not None:
            errors.append(f"triageIntent must be omitted when triageResult is {result}")

    if errors:
        return None, errors
    return {"triage_result": result, "triage_intent": intent, "triaged_by": by}, []


def validate_paging(status: str, limit: int, offset: int) -> list[str]:
    errors = []
    if status not in STATUSES:
        errors.append(f"status must be one of {', '.join(STATUSES)}")
    if not (1 <= limit <= MAX_LIMIT):
        errors.append(f"limit must be between 1 and {MAX_LIMIT}")
    if offset < 0:
        errors.append("offset must be 0 or greater")
    return errors


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else None


def row_to_item(row) -> dict:
    return {
        "id": row.id,
        "conversationId": row.conversation_id,
        "experienceId": str(row.experience_id) if row.experience_id is not None else None,
        "requestId": row.request_id,
        "normalizedMessage": row.normalized_message,
        "bestSimilarity": float(row.best_similarity) if row.best_similarity is not None else None,
        "bestIntent": row.best_intent,
        "noveltyThreshold": float(row.novelty_threshold),
        "brainVersion": row.brain_version,
        "triaged": bool(row.triaged),
        "triageResult": row.triage_result,
        "triageIntent": row.triage_intent,
        "triagedAt": _iso(row.triaged_at),
        "triagedBy": row.triaged_by,
        "createdAt": _iso(row.created_at),
    }


# ── Routes ───────────────────────────────────────────────────────────

@router.get("/tenants/{tenant_id}/novelty-events")
async def list_novelty_events(
    tenant_id: int,
    status: str = Query("untriaged"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Novelty events for one tenant, newest first. Default view is the
    untriaged queue. `total` is the count for the same filter, so a UI
    can page."""
    errors = validate_paging(status, limit, offset)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    clause = _STATUS_CLAUSE[status]
    total_row = (await db.execute(
        text(f"SELECT COUNT(*) AS n FROM novelty_events WHERE tenant_id = :tenant_id {clause}"),
        {"tenant_id": tenant_id},
    )).first()
    total = int(total_row.n) if total_row is not None and total_row.n is not None else 0

    result = await db.execute(
        text(f"""
            SELECT {_COLUMNS}
              FROM novelty_events
             WHERE tenant_id = :tenant_id {clause}
             ORDER BY created_at DESC, id DESC
             LIMIT :limit OFFSET :offset
        """),
        {"tenant_id": tenant_id, "limit": limit, "offset": offset},
    )
    items = [row_to_item(r) for r in result]
    return {
        "tenantId": tenant_id,
        "status": status,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": items,
    }


@router.patch("/novelty-events/{event_id}")
async def triage_novelty_event(
    event_id: int,
    body: TriageBody,
    tenant_id: int = Query(..., alias="tenantId"),
    db: AsyncSession = Depends(get_db),
):
    """Record a triage verdict. Re-triaging an already-triaged event is
    allowed and overwrites the previous verdict (triaged_at/by updated);
    the log line keeps the previous values."""
    clean, errors = validate_triage(body.model_dump())
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    previous = (await db.execute(
        text("""
            SELECT triaged, triage_result, triage_intent, triaged_by
              FROM novelty_events
             WHERE id = :event_id AND tenant_id = :tenant_id
        """),
        {"event_id": event_id, "tenant_id": tenant_id},
    )).first()
    if previous is None:
        raise HTTPException(
            status_code=404,
            detail=f"novelty event {event_id} not found for tenant {tenant_id}",
        )

    updated = (await db.execute(
        text(f"""
            UPDATE novelty_events
               SET triaged       = TRUE,
                   triage_result = :triage_result,
                   triage_intent = :triage_intent,
                   triaged_at    = NOW(),
                   triaged_by    = :triaged_by
             WHERE id = :event_id AND tenant_id = :tenant_id
         RETURNING {_COLUMNS}
        """),
        {**clean, "event_id": event_id, "tenant_id": tenant_id},
    )).first()
    await db.commit()

    if updated is None:  # deleted between the two statements
        raise HTTPException(
            status_code=404,
            detail=f"novelty event {event_id} not found for tenant {tenant_id}",
        )

    logger.info(
        "admin novelty triage tenant=%s event=%s result=%s intent=%s by=%s "
        "previous_result=%s previous_intent=%s",
        tenant_id, event_id, clean["triage_result"], clean["triage_intent"],
        clean["triaged_by"],
        previous.triage_result if previous.triaged else None,
        previous.triage_intent if previous.triaged else None,
    )
    return row_to_item(updated)
