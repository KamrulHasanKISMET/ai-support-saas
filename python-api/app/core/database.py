from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

# PostgreSQL remains the single source of truth (architecture doc
# section 8). This engine is shared across the whole AI Service.
engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)

AsyncSessionLocal = async_sessionmaker(
    bind=engine, expire_on_commit=False, class_=AsyncSession
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a request-scoped DB session."""
    async with AsyncSessionLocal() as session:
        yield session


async def check_database_health() -> dict:
    """
    Cheapest possible liveness probe for Postgres: `SELECT 1` (no table
    scan, no real query, matches the task's "do not run expensive
    database queries" constraint exactly). Used by GET /readiness --
    never by request-serving code paths.

    Also reports the connection pool's current checked-out/available
    counts (already tracked by SQLAlchemy's pool internally -- reading
    them costs nothing, no extra query). Pool-stat introspection is
    wrapped separately from the connectivity check itself, so a pool
    API quirk can never be mistaken for "the database is down".
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        return {"available": False, "error": str(exc)}

    result = {"available": True}
    try:
        pool = engine.pool
        result["pool_size"] = pool.size()
        result["pool_checked_out"] = pool.checkedout()
    except Exception:
        pass
    return result
