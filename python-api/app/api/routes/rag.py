from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_internal_secret
from app.rag.hybrid_search import hybrid_search
from app.rag.reranker import reranker

router = APIRouter(prefix="/rag", tags=["rag"], dependencies=[Depends(require_internal_secret)])


@router.get("/search")
async def search(
    tenant_id: int = Query(..., alias="tenantId"),
    q: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
):
    """
    Debug endpoint: run hybrid search + rerank directly, without going
    through the full Kernel. Useful while tuning ingestion/chunking
    before wiring the Context Engine end-to-end.
    """
    candidates = await hybrid_search.search(db, tenant_id, q)
    top = reranker.rerank(candidates)
    return {"query": q, "results": top}
