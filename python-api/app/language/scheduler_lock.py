"""
Single-run guard for the out-of-band language jobs (canary ramp,
calibration). Two overlapping runs of the same job are unsafe:
CalibrationService documents "avoid concurrent calls for the same
tenant (no distributed lock exists yet)", and two ramp runs could
double-step a canary. This is that lock.

Mechanism: a Postgres SESSION-level advisory lock held on a DEDICATED
connection for the whole job. It is not taken on the job's own
AsyncSession because those sessions commit constantly and may hand the
connection back to the pool between statements, which would silently
drop or leak a session-level lock.

    async with advisory_job_lock("canary_ramp") as acquired:
        if not acquired:
            return  # another run holds it -- skip, do not queue
        ...

The lock is released in `finally`; if the process dies, Postgres drops
it when the connection closes. It guards against overlapping RUNS of
the same named job only -- it is not a per-tenant lock and does not
protect against a human calling a service method by hand.

Not verified against a real Postgres in the build sandbox (no DB);
the key derivation and control flow are unit-tested with a fake
connection. Try it once in Docker: run the same job twice at once and
confirm the second one reports "skipped".
"""

import hashlib
from contextlib import asynccontextmanager

from sqlalchemy import text

from app.core.logging import logger


def lock_key(name: str) -> int:
    """Stable signed 64-bit key for pg_try_advisory_lock(bigint)."""
    digest = hashlib.sha256(f"ai-support-saas:{name}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@asynccontextmanager
async def advisory_job_lock(name: str, *, connect=None):
    """
    Yields True when this caller now holds the lock, False when another
    run holds it. `connect` is an async-context-manager factory returning
    a connection (defaults to the app engine's `engine.connect`); it
    exists so tests can inject a fake without a database.
    """
    if connect is None:
        from app.core.database import engine  # lazy: keeps import cheap for tests

        connect = engine.connect

    key = lock_key(name)
    async with connect() as conn:
        result = await conn.execute(
            text("SELECT pg_try_advisory_lock(:key) AS acquired"), {"key": key}
        )
        acquired = bool(result.scalar())
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    await conn.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                    )
                except Exception:
                    # Connection is closing anyway; Postgres releases the
                    # lock with the session. Log, never raise from cleanup.
                    logger.error("advisory unlock failed job=%s", name, exc_info=True)
