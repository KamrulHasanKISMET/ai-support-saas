import time
from collections.abc import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.metrics import db_query_duration_seconds

# PostgreSQL remains the single source of truth (architecture doc
# section 8). This engine is shared across the whole AI Service.
#
# Production Reliability workstream: explicit pool sizing/recycle
# (previously all SQLAlchemy defaults) — see docs/RELIABILITY.md
# "Scaling triggers" for how this relates to Postgres's own
# max_connections across both services.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout_seconds,
    pool_recycle=settings.db_pool_recycle_seconds,
)

# DB query latency, timed via the DBAPI-level cursor events every query
# on this engine already goes through — no need to touch individual
# repository/engine call sites. Mirrors node-api's single-choke-point
# `pool.query` wrapper (config/database.ts) for the same metric name's
# intent. These are sync events fired by SQLAlchemy's underlying sync
# DBAPI layer (even under the asyncpg async dialect), so no await here.
_query_start_times: dict[int, float] = {}


@event.listens_for(engine.sync_engine, "before_cursor_execute")
def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
    _query_start_times[id(cursor)] = time.perf_counter()


@event.listens_for(engine.sync_engine, "after_cursor_execute")
def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
    start = _query_start_times.pop(id(cursor), None)
    if start is not None:
        db_query_duration_seconds.labels(status="ok").observe(time.perf_counter() - start)


@event.listens_for(engine.sync_engine, "handle_error")
def _handle_error(exception_context) -> None:  # noqa: ANN001
    cursor = getattr(exception_context, "cursor", None)
    start = _query_start_times.pop(id(cursor), None) if cursor is not None else None
    if start is not None:
        db_query_duration_seconds.labels(status="error").observe(time.perf_counter() - start)


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

    Preserved from the pre-Reliability implementation: the Production
    Reliability workstream's own draft of this file dropped this
    function in favor of Prometheus-only pool gauges, but GET /readiness
    (api/routes/readiness.py) depends on the richer available/error
    shape this returns, so it stays -- the Prometheus gauges set in
    api/routes/metrics.py are additive, not a replacement.
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
