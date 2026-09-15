from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class MemorySearch:
    """Memory search MUST enforce tenant_id + customer_id (section 15)."""

    async def get_all(
        self, db: AsyncSession, tenant_id: int, customer_id: int
    ) -> list[dict]:
        rows = await db.execute(
            text(
                """
                SELECT memory_type, memory_key, memory_value, confidence
                  FROM customer_memories
                 WHERE tenant_id = :tenant_id AND customer_id = :customer_id
                 ORDER BY updated_at DESC
                """
            ),
            {"tenant_id": tenant_id, "customer_id": customer_id},
        )
        return [dict(row._mapping) for row in rows]

    async def semantic_search(
        self,
        db: AsyncSession,
        tenant_id: int,
        customer_id: int,
        query_embedding: list[float],
        limit: int = 5,
    ) -> list[dict]:
        rows = await db.execute(
            text(
                """
                SELECT memory_key, memory_value,
                       1 - (embedding <=> :query_embedding) AS similarity
                  FROM customer_memories
                 WHERE tenant_id = :tenant_id AND customer_id = :customer_id
                   AND embedding IS NOT NULL
                 ORDER BY embedding <=> :query_embedding
                 LIMIT :limit
                """
            ),
            {
                "tenant_id": tenant_id,
                "customer_id": customer_id,
                "query_embedding": str(query_embedding),
                "limit": limit,
            },
        )
        return [dict(row._mapping) for row in rows]


memory_search = MemorySearch()
