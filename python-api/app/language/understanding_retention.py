"""
Retention job for turn_understandings (90 days). Idempotent; safe to run
as often as you like.

    docker compose exec python-api python -m app.language.understanding_retention --dry-run
    docker compose exec python-api python -m app.language.understanding_retention

Schedule it daily (host cron / pg_cron / your orchestrator) -- the app
does not, see docs/PENDING_WORK.md C1a. Exit 0 on success, 1 on error.
"""

from __future__ import annotations

import argparse
import asyncio


async def _main(dry_run: bool) -> int:
    from app.core.database import AsyncSessionLocal
    from app.language.understanding_store import TURN_UNDERSTANDING_RETENTION_DAYS, purge_expired

    async with AsyncSessionLocal() as db:
        n = await purge_expired(db, dry_run=dry_run)
    verb = "would delete" if dry_run else "deleted"
    print(f"turn_understandings: {verb} {n} rows older than {TURN_UNDERSTANDING_RETENTION_DAYS} days")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Purge expired turn_understandings rows.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_main(args.dry_run)))
