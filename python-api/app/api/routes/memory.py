from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_internal_secret
from app.memory.memory_search import memory_search

router = APIRouter(prefix="/memory", tags=["memory"], dependencies=[Depends(require_internal_secret)])


@router.get("/")
async def list_memories(
    tenant_id: int = Query(..., alias="tenantId"),
    customer_id: int = Query(..., alias="customerId"),
    db: AsyncSession = Depends(get_db),
):
    """Debug/dashboard endpoint: view everything remembered about a customer."""
    memories = await memory_search.get_all(db, tenant_id, customer_id)
    return {"tenantId": tenant_id, "customerId": customer_id, "memories": memories}
