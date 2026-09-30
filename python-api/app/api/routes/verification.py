"""
Admin: human verification of language_experiences rows.

docs/GENERAL_LANGUAGE_BRAIN.md §2.2/§2.4/§9 item 2. Same protection
model as rag.py/memory.py's debug routes: an internal operator tool
with no separate dashboard-permission layer yet (docs/ROADMAP.md §1's
"role-based permission checks on dashboard routes" gap applies here
too), gated for now by the existing node-api<->python-api internal
secret rather than left open.
"""

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_internal_secret
from app.language import verification_service

router = APIRouter(
    prefix="/language/experiences",
    tags=["verification"],
    dependencies=[Depends(require_internal_secret)],
)


class CorrectionBody(BaseModel):
    corrected_intent: str


@router.post("/{experience_id}/confirm")
async def confirm(
    experience_id: str = Path(...),
    tenant_id: int = Query(..., alias="tenantId"),
    db: AsyncSession = Depends(get_db),
):
    """A human reviewed this row's final_intent and it was correct."""
    ok = await verification_service.confirm_experience(db, tenant_id, experience_id)
    if not ok:
        raise HTTPException(status_code=404, detail="experience not found for tenant")
    return {"experienceId": experience_id, "verificationLevel": "human_confirmed"}


@router.post("/{experience_id}/correct")
async def correct(
    body: CorrectionBody,
    experience_id: str = Path(...),
    tenant_id: int = Query(..., alias="tenantId"),
    db: AsyncSession = Depends(get_db),
):
    """A human reviewed this row's final_intent and it was wrong. Writes
    a new superseding row rather than editing this one — see
    verification_service.correct_experience()."""
    new_id = await verification_service.correct_experience(
        db, tenant_id, experience_id, body.corrected_intent
    )
    if new_id is None:
        raise HTTPException(status_code=404, detail="experience not found for tenant")
    return {
        "supersededExperienceId": experience_id,
        "newExperienceId": new_id,
        "finalIntent": body.corrected_intent,
        "verificationLevel": "human_corrected",
    }
