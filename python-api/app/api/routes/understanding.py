"""
Admin: per-tenant understanding report (read-only).

Same protection model as verification.py / rag.py / memory.py: internal
operator tool gated by the node-api<->python-api internal secret, with
no dashboard role layer yet (docs/ROADMAP.md §1 gap applies).
Tenant-scoped by a required tenantId query parameter.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_internal_secret
from app.language import understanding_report

router = APIRouter(
    prefix="/language/understanding",
    tags=["understanding"],
    dependencies=[Depends(require_internal_secret)],
)


@router.get("/report")
async def report(
    tenant_id: int = Query(..., alias="tenantId"),
    days: int = Query(30, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
):
    """What the Brain has been seeing for this tenant over the last
    `days` (max 90 = the table's retention)."""
    return await understanding_report.fetch(db, tenant_id, days)
