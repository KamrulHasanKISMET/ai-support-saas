from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.embedding_service import embedding_service
from app.rag.search import keyword_search, vector_search


class HybridSearch:
    """Question -> [Vector Search, Keyword Search] -> Combine -> Dedupe
    (architecture doc section 11). Re-ranking happens downstream in
    app/rag/reranker.py — this stage just gathers candidates."""

    async def search(
        self, db: AsyncSession, tenant_id: int, query: str, limit: int = 20
    ) -> list[dict]:
        query_embedding = await embedding_service.embed(query)

        vector_results = await vector_search.search(db, tenant_id, query_embedding, limit)
        keyword_results = await keyword_search.search(db, tenant_id, query, limit)

        combined: dict[int, dict] = {}
        for r in vector_results:
            combined[r["id"]] = {**r, "vector_score": r.get("similarity", 0.0), "keyword_score": 0.0}
        for r in keyword_results:
            if r["id"] in combined:
                combined[r["id"]]["keyword_score"] = r.get("rank", 0.0)
            else:
                combined[r["id"]] = {**r, "vector_score": 0.0, "keyword_score": r.get("rank", 0.0)}

        return list(combined.values())


hybrid_search = HybridSearch()
