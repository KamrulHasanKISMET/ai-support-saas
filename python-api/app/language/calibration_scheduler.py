"""
CALIBRATION SCHEDULER (Phase 4 completion, step 4)

docs/LANGUAGE_INTELLIGENCE.md: "Calibration schedule still manual." This
is the thin out-of-band wrapper that calls
CalibrationService.run_for_tenant() for every active tenant. It is NOT
started by the app -- run it from host cron or a compose command:

    docker compose exec -T python-api python -m app.language.calibration_scheduler --dry-run
    docker compose exec -T python-api python -m app.language.calibration_scheduler
    docker compose exec -T python-api python -m app.language.calibration_scheduler --tenant 3
    docker compose exec -T python-api python -m app.language.calibration_scheduler --loop --interval-hours 24

Ready-to-paste host cron (daily 03:45, from the compose directory; the
retention job in PENDING_WORK.md C1a uses 03:15):
    45 3 * * * cd /path/to/project && docker compose exec -T python-api python -m app.language.calibration_scheduler >> /var/log/calibration_scheduler.log 2>&1

Exit code 0 = ran (or skipped because another run holds the lock),
1 = an explicitly requested tenant was not found/active, or the run
itself failed.

What it does per tenant: hands the tenant to CalibrationService (which
loads that tenant's tenant_calibration_config itself, so per-tenant
thresholds apply automatically). Each tenant gets a fresh DB session and
its own try/except; one tenant failing never blocks the others.

Things to know before you schedule it:

  * It CHANGES LIVE ROUTING. CalibrationService rewrites
    calibrated_threshold on intent_clusters, and NULLs it for an intent
    whose agreement fell below the bar -- so a daily run can move an
    intent from brain back to the LLM (that is the intended safety
    behaviour, but it is not a no-op). Run --dry-run first to see which
    tenants have data.
  * It cannot tell "no shadow data" from "calibration failed". The
    service swallows its own fatal errors and returns an empty result
    (logged at ERROR by CalibrationService), so a tenant with
    total_samples == 0 in the summary means EITHER; check the logs.
  * --dry-run does NOT call CalibrationService (it has no dry-run mode
    and writes). It lists the tenants that would be calibrated with
    their brain_used=TRUE row counts, and writes nothing.
  * Overlapping runs are prevented with a Postgres advisory lock
    (scheduler_lock.py); the loser reports "skipped".
  * --loop runs one pass immediately on start, then every
    --interval-hours. A container restart therefore triggers a pass;
    calibration is idempotent so that is harmless.
  * Calibration is not tied to cluster promotion: a newly promoted
    cluster is calibrated on the next pass, not immediately
    (LANGUAGE_INTELLIGENCE.md Phase 3 step 3 remains open).
"""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import text

from app.core.logging import logger
from app.language.calibration_service import calibration_service
from app.language.scheduler_lock import advisory_job_lock

LOCK_NAME = "calibration"
DEFAULT_INTERVAL_HOURS = 24.0

_ACTIVE_TENANTS = "SELECT id FROM tenants WHERE is_active = TRUE ORDER BY id"
_ACTIVE_TENANT_ONE = "SELECT id FROM tenants WHERE is_active = TRUE AND id = :tenant_id ORDER BY id"
_SHADOW_ROW_COUNT = """
    SELECT COUNT(*) AS n
      FROM language_experiences
     WHERE tenant_id = :tenant_id
       AND brain_used = TRUE
       AND final_intent IS NOT NULL
       AND brain_prediction IS NOT NULL
"""


def result_to_dict(result) -> dict:
    """CalibrationResult -> JSON-able dict."""
    return {
        "tenantId": result.tenant_id,
        "totalSamples": result.total_samples,
        "intentsUpdated": result.intents_updated,
        "perIntent": [
            {
                "intent": s.intent,
                "sampleCount": s.sample_count,
                "similarityThreshold": s.similarity_threshold,
                "agreementRate": s.agreement_rate,
            }
            for s in result.per_intent
        ],
    }


async def run_all(
    *,
    tenant_id: int | None = None,
    dry_run: bool = False,
    session_factory=None,
    lock=None,
) -> dict:
    """One pass over every active tenant (or just `tenant_id`). Returns
    a JSON-able summary; `skipped` is True when another run holds the
    lock. Never raises for a single tenant's failure."""
    if session_factory is None:
        from app.core.database import AsyncSessionLocal as session_factory
    lock = lock or advisory_job_lock

    async with lock(LOCK_NAME) as acquired:
        if not acquired:
            logger.info("CalibrationScheduler: another run holds the lock -- skipping")
            return {"skipped": True, "dryRun": dry_run, "errors": 0, "tenants": []}

        async with session_factory() as db:
            if tenant_id is None:
                rows = await db.execute(text(_ACTIVE_TENANTS))
            else:
                rows = await db.execute(text(_ACTIVE_TENANT_ONE), {"tenant_id": tenant_id})
            tenant_ids = [r.id for r in rows]

        if tenant_id is not None and not tenant_ids:
            msg = f"tenant {tenant_id} not found or not active"
            logger.warning("CalibrationScheduler: %s", msg)
            return {"skipped": False, "dryRun": dry_run, "errors": 1, "error": msg, "tenants": []}

        tenants: list[dict] = []
        errors = 0
        for tid in tenant_ids:
            try:
                async with session_factory() as db:  # fresh session per tenant
                    if dry_run:
                        row = (await db.execute(
                            text(_SHADOW_ROW_COUNT), {"tenant_id": tid}
                        )).first()
                        n = int(row.n) if row is not None and row.n is not None else 0
                        tenants.append({"tenantId": tid, "wouldCalibrate": n > 0, "shadowRows": n})
                    else:
                        result = await calibration_service.run_for_tenant(db, tid)
                        tenants.append(result_to_dict(result))
            except Exception:
                errors += 1
                logger.error("CalibrationScheduler failed tenant=%s", tid, exc_info=True)
                tenants.append({"tenantId": tid, "error": "unexpected error (see logs)"})

    logger.info(
        "CalibrationScheduler done tenants=%d errors=%d dry_run=%s",
        len(tenant_ids), errors, dry_run,
    )
    return {"skipped": False, "dryRun": dry_run, "errors": errors, "tenants": tenants}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run confidence calibration for all active tenants.")
    ap.add_argument("--tenant", type=int, help="only this tenant id")
    ap.add_argument("--dry-run", action="store_true",
                    help="list tenants with shadow data; write nothing, do not calibrate")
    ap.add_argument("--loop", action="store_true", help="repeat forever (container stop to end)")
    ap.add_argument("--interval-hours", type=float, default=DEFAULT_INTERVAL_HOURS, help="with --loop")
    args = ap.parse_args(argv)

    async def once() -> int:
        summary = await run_all(tenant_id=args.tenant, dry_run=args.dry_run)
        print(json.dumps(summary, indent=2, default=str))
        return 1 if summary["errors"] else 0

    async def loop() -> int:
        worst = 0
        while True:
            try:
                worst = max(worst, await once())
            except Exception:
                logger.error("CalibrationScheduler loop iteration failed", exc_info=True)
                worst = 1
            await asyncio.sleep(args.interval_hours * 3600)

    return asyncio.run(loop() if args.loop else once())


if __name__ == "__main__":
    raise SystemExit(main())
