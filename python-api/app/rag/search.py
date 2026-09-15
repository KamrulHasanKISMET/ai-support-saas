from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class VectorSearch:
    """
    Semantic search over knowledge_chunks. Every query filters by
    tenant_id — vector retrieval MUST never cross tenant boundaries
    (architecture doc section 10).
    """

    async def search(
        self,
        db: AsyncSession,
        tenant_id: int,
        query_embedding: list[float],
        limit: int = 20,
    ) -> list[dict]:
        rows = await db.execute(
            text(
                """
                SELECT id, document_id, content, category,
                       1 - (embedding <=> :query_embedding) AS similarity
                  FROM knowledge_chunks
                 WHERE tenant_id = :tenant_id
                   AND embedding IS NOT NULL
                 ORDER BY embedding <=> :query_embedding
                 LIMIT :limit
                """
            ),
            {
                "tenant_id": tenant_id,
                "query_embedding": str(query_embedding),
                "limit": limit,
            },
        )
        return [dict(row._mapping) for row in rows]


class KeywordSearch:
    """
    Plain keyword/full-text search — important for SKUs, order IDs,
    exact product names, and technical terms that embeddings can blur
    (architecture doc section 11).
    """

    async def search(
        self, db: AsyncSession, tenant_id: int, query: str, limit: int = 20
    ) -> list[dict]:
        rows = await db.execute(
            text(
                """
                SELECT id, document_id, content, category,
                       ts_rank_cd(to_tsvector('simple', content), plainto_tsquery('simple', :query)) AS rank
                  FROM knowledge_chunks
                 WHERE tenant_id = :tenant_id
                   AND to_tsvector('simple', content) @@ plainto_tsquery('simple', :query)
                 ORDER BY rank DESC
                 LIMIT :limit
                """
            ),
            {"tenant_id": tenant_id, "query": query, "limit": limit},
        )
        return [dict(row._mapping) for row in rows]


vector_search = VectorSearch()
keyword_search = KeywordSearch()
