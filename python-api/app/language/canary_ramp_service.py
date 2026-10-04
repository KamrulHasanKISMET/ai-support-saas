"""
CANARY RAMP SERVICE (Phase 4 completion, step 3)

docs/LANGUAGE_INTELLIGENCE.md: "canary_pct is set once by
PromotionService.start_canary() and never auto-incremented." This is
the out-of-band job that steps it 5 -> 20 -> 50 -> 100 while accuracy
holds, and hands the canary to PromotionService only once it is at 100%.

Run it (cron / compose command); it is NOT started by the app:

    docker compose exec -T python-api python -m app.language.canary_ramp_service --dry-run
    docker compose exec -T python-api python -m app.language.canary_ramp_service
    docker compose exec -T python-api python -m app.language.canary_ramp_service --loop --interval-minutes 60

Exit code 0 = ran (or skipped because another run holds the lock),
1 = at least one canary/tenant errored.

Per active canary (canary_splits row), per run:

  stage evidence = brain-routed routing_decisions for the intent with a
                   known was_correct since the START OF THE CURRENT
                   STAGE (canary_splits.updated_at, reset on every step
                   and on start_canary()). A stage never inherits the
                   previous stage's evidence.

  decide_ramp() (pure, unit-tested):
    stage_samples < MIN_STAGE_SAMPLES        -> hold  (need more data)
    stage_rate   < min_rollback_accuracy     -> abort (retire candidate;
                                                logged 'canary_fail')
    stage age    < min_dwell_hours           -> hold  (let time-of-day
                                                traffic vary; bad news
                                                above is NOT delayed)
    stage_rate  >= min_promote_accuracy      -> advance to next stage, or
                                                at 100% -> hand to
                                                PromotionService.check_and_promote()
    otherwise                                -> hold  (between thresholds)

PromotionService stays the only thing that PROMOTES (it also runs the
§5.5 generalization gate). This service only ever calls it at the final
(100%) stage. Reaching 100% also restarts the canary's evidence window
(canary_splits.started_at = NOW()) so PromotionService's whole-canary
accuracy is measured on full-traffic data only, matching this service's
stage numbers instead of being diluted by the small early stages.
Thresholds (min_promote_accuracy, min_rollback_accuracy) come from
tenant_calibration_config per tenant.

  Once this job is in use, do not also call
  PromotionService.check_and_promote() by hand on a ramping canary: it
  measures the whole canary window and can promote at 5%.

FIXED (PENDING_WORK C8, db/init/019): routing_decisions now records the
serving cluster (served_cluster_id) and its role (served_branch), and the
stage evidence counts ONLY rows served by this canary's candidate cluster.
Every stage is now a measurement of the candidate itself, not a blend with
the promoted cluster. Consequences to know: rows written before migration
019 are NULL and are never counted, so an in-flight canary restarts its
stage evidence from zero after deploy; and at low percentages the 20-sample
minimum takes proportionally longer to reach (5% of traffic).
PromotionService.check_and_promote() still measures the whole window
unfiltered -- fine at 100% (all traffic is candidate), which is the only
stage at which the ramp calls it.

Also: a canary whose candidate cluster has no calibrated_threshold gets
no traffic (RoutingService ignores it), so it can never gather samples;
this job reports that reason instead of waiting silently.

Policy constants below are choices, not derived from data: the ladder
(from the design doc), MIN_STAGE_SAMPLES (= PromotionService's canary
floor) and MIN_STAGE_DWELL_HOURS (24h, so a busy tenant cannot reach
100% within an hour).

Isolation: every canary is handled in its own try/except; one failure
never blocks the others. Overlapping runs are prevented with a Postgres
advisory lock (scheduler_lock.py).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.language.promotion_service import MIN_CANARY_SAMPLES, promotion_service
from app.language.scheduler_lock import advisory_job_lock
from app.language.tenant_calibration_config import TenantCalibrationConfig

RAMP_STAGES: tuple[float, ...] = (5.0, 20.0, 50.0, 100.0)
FINAL_STAGE_PCT: float = RAMP_STAGES[-1]
MIN_STAGE_SAMPLES: int = MIN_CANARY_SAMPLES
MIN_STAGE_DWELL_HOURS: float = 24.0
LOCK_NAME = "canary_ramp"

# Actions decide_ramp() can return.
HOLD_SAMPLES = "hold_insufficient_samples"
HOLD_DWELL = "hold_dwell"
HOLD_BETWEEN = "hold_between_thresholds"
HOLD_UNCALIBRATED = "hold_candidate_uncalibrated"
ADVANCE = "advance"
ABORT = "abort"
PROMOTE_CHECK = "promote_check"
ERROR = "error"


@dataclass(frozen=True)
class RampDecision:
    action: str
    next_pct: float | None = None
    reason: str = ""


@dataclass
class RampOutcome:
    intent: str
    action: str
    from_pct: float | None = None
    to_pct: float | None = None
    stage_samples: int | None = None
    stage_rate: float | None = None
    stage_age_hours: float | None = None
    reason: str = ""
    applied: bool = False              # False on --dry-run, and on holds
    promotion_action: str | None = None  # PromotionService result when handed over

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RampRunResult:
    tenant_id: int
    outcomes: list[RampOutcome]

    @property
    def errors(self) -> int:
        return sum(1 for o in self.outcomes if o.action == ERROR)

    def to_dict(self) -> dict:
        return {
            "tenantId": self.tenant_id,
            "errors": self.errors,
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


# ── Pure decision logic ──────────────────────────────────────────────

def next_stage(pct: float) -> float | None:
    """Smallest ladder stage strictly above pct, or None at/after 100."""
    for stage in RAMP_STAGES:
        if stage > pct + 1e-9:
            return stage
    return None


def is_final(pct: float) -> bool:
    return pct >= FINAL_STAGE_PCT - 1e-9


def decide_ramp(
    *,
    pct: float,
    stage_samples: int,
    stage_rate: float | None,
    stage_age_hours: float,
    min_promote_accuracy: float,
    min_rollback_accuracy: float,
    min_samples: int = MIN_STAGE_SAMPLES,
    min_dwell_hours: float = MIN_STAGE_DWELL_HOURS,
) -> RampDecision:
    """See the module docstring for the ordering; it is deliberate:
    insufficient data always holds, but a confirmed regression aborts
    without waiting out the dwell time."""
    if stage_samples < min_samples or stage_rate is None:
        return RampDecision(HOLD_SAMPLES, reason=f"{stage_samples}/{min_samples} samples in this stage")

    if stage_rate < min_rollback_accuracy:
        return RampDecision(
            ABORT,
            reason=f"stage accuracy {stage_rate:.3f} < rollback threshold "
                   f"{min_rollback_accuracy:.2f} at {pct:g}%",
        )

    if stage_age_hours < min_dwell_hours:
        return RampDecision(
            HOLD_DWELL,
            reason=f"stage is {stage_age_hours:.1f}h old, minimum {min_dwell_hours:g}h",
        )

    if stage_rate >= min_promote_accuracy:
        if is_final(pct):
            return RampDecision(PROMOTE_CHECK, reason=f"stage accuracy {stage_rate:.3f} at 100%")
        nxt = next_stage(pct)
        return RampDecision(
            ADVANCE, next_pct=nxt,
            reason=f"stage accuracy {stage_rate:.3f} >= {min_promote_accuracy:.2f}",
        )

    return RampDecision(
        HOLD_BETWEEN,
        reason=f"stage accuracy {stage_rate:.3f} between rollback "
               f"{min_rollback_accuracy:.2f} and promote {min_promote_accuracy:.2f}",
    )


# ── SQL (fixed strings; only bound parameters vary) ──────────────────

_LIST_CANARIES = """
    SELECT cs.intent, cs.candidate_cluster_id, cs.canary_pct, cs.updated_at,
           EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - cs.updated_at)) / 3600.0 AS stage_age_hours,
           (ic.calibrated_threshold IS NOT NULL) AS candidate_calibrated
      FROM canary_splits cs
      JOIN intent_clusters ic ON ic.id = cs.candidate_cluster_id
     WHERE cs.tenant_id = :tenant_id
     ORDER BY cs.intent
"""

_STAGE_STATS = """
    SELECT COUNT(*) AS n,
           AVG(CASE WHEN was_correct THEN 1.0 ELSE 0.0 END) AS rate
      FROM routing_decisions
     WHERE tenant_id        = :tenant_id
       AND predicted_intent = :intent
       AND routed_to        = 'brain'
       AND was_correct      IS NOT NULL
       AND created_at       >= :since
       AND served_branch    = 'candidate'
       AND served_cluster_id = :cluster_id
"""

_ADVANCE = """
    UPDATE canary_splits
       SET canary_pct = :next_pct,
           updated_at = NOW()
     WHERE tenant_id = :tenant_id
       AND intent = :intent
       AND candidate_cluster_id = :cluster_id
       AND canary_pct = :old_pct
"""

# Reaching 100% also restarts the evidence window (see module docstring).
_ADVANCE_TO_FINAL = """
    UPDATE canary_splits
       SET canary_pct = :next_pct,
           updated_at = NOW(),
           started_at = NOW()
     WHERE tenant_id = :tenant_id
       AND intent = :intent
       AND candidate_cluster_id = :cluster_id
       AND canary_pct = :old_pct
"""

_LOG_EVENT = """
    INSERT INTO cluster_promotion_log (
        tenant_id, intent, cluster_id, version, event_type,
        sample_count, was_correct_rate, canary_pct, notes
    ) VALUES (
        :tenant_id, :intent, :cluster_id, NULL, 'canary_ramp',
        :sample_count, :was_correct_rate, :canary_pct, :notes
    )
"""

_TENANTS_WITH_CANARIES = """
    SELECT DISTINCT cs.tenant_id
      FROM canary_splits cs
      JOIN tenants t ON t.id = cs.tenant_id
     WHERE t.is_active = TRUE
     ORDER BY cs.tenant_id
"""
_TENANT_ONE_WITH_CANARIES = """
    SELECT DISTINCT cs.tenant_id
      FROM canary_splits cs
      JOIN tenants t ON t.id = cs.tenant_id
     WHERE t.is_active = TRUE AND cs.tenant_id = :tenant_id
     ORDER BY cs.tenant_id
"""


class CanaryRampService:
    """Stateless; module-level singleton `canary_ramp_service` below."""

    async def run_for_tenant(
        self,
        db: AsyncSession,
        tenant_id: int,
        *,
        dry_run: bool = False,
        min_dwell_hours: float = MIN_STAGE_DWELL_HOURS,
    ) -> RampRunResult:
        """Evaluate (and, unless dry_run, act on) every active canary of
        one tenant. Never raises: a failure on one canary is reported as
        an ERROR outcome and the rest still run."""
        cfg = await TenantCalibrationConfig.load(db, tenant_id)
        try:
            result = await db.execute(text(_LIST_CANARIES), {"tenant_id": tenant_id})
            canaries = list(result)
        except Exception:
            logger.error("CanaryRamp: listing canaries failed tenant=%s", tenant_id, exc_info=True)
            return RampRunResult(
                tenant_id, [RampOutcome(intent="*", action=ERROR, reason="could not list canaries")]
            )

        outcomes: list[RampOutcome] = []
        for row in canaries:
            try:
                outcomes.append(
                    await self._ramp_one(db, tenant_id, row, cfg, dry_run, min_dwell_hours)
                )
            except Exception:
                logger.error(
                    "CanaryRamp failed tenant=%s intent=%s", tenant_id, row.intent, exc_info=True
                )
                await self._safe_rollback(db)
                outcomes.append(RampOutcome(
                    intent=row.intent, action=ERROR, from_pct=float(row.canary_pct),
                    reason="unexpected error (see logs)",
                ))
        return RampRunResult(tenant_id, outcomes)

    async def _ramp_one(
        self, db: AsyncSession, tenant_id: int, row, cfg: TenantCalibrationConfig,
        dry_run: bool, min_dwell_hours: float,
    ) -> RampOutcome:
        intent = row.intent
        pct = float(row.canary_pct)
        age = float(row.stage_age_hours) if row.stage_age_hours is not None else 0.0
        out = RampOutcome(intent=intent, action=HOLD_SAMPLES, from_pct=pct, stage_age_hours=round(age, 2))

        if not row.candidate_calibrated:
            out.action = HOLD_UNCALIBRATED
            out.reason = ("candidate cluster has no calibrated_threshold, so RoutingService "
                          "sends it no traffic; run calibration for this tenant")
            return out

        stats = (await db.execute(
            text(_STAGE_STATS),
            {"tenant_id": tenant_id, "intent": intent, "since": row.updated_at,
             "cluster_id": row.candidate_cluster_id},
        )).first()
        samples = int(stats.n) if stats is not None and stats.n else 0
        rate = float(stats.rate) if stats is not None and stats.rate is not None else None
        out.stage_samples, out.stage_rate = samples, (round(rate, 4) if rate is not None else None)

        decision = decide_ramp(
            pct=pct, stage_samples=samples, stage_rate=rate, stage_age_hours=age,
            min_promote_accuracy=cfg.min_promote_accuracy,
            min_rollback_accuracy=cfg.min_rollback_accuracy,
            min_dwell_hours=min_dwell_hours,
        )
        out.action, out.reason, out.to_pct = decision.action, decision.reason, decision.next_pct
        if decision.action == PROMOTE_CHECK:
            out.to_pct = pct

        if decision.action not in (ADVANCE, ABORT, PROMOTE_CHECK):
            logger.info("CanaryRamp hold tenant=%s intent=%s pct=%g: %s",
                        tenant_id, intent, pct, decision.reason)
            return out
        if dry_run:
            logger.info("CanaryRamp DRY-RUN tenant=%s intent=%s would %s: %s",
                        tenant_id, intent, decision.action, decision.reason)
            return out

        if decision.action == ABORT:
            res = await promotion_service.abort_canary(
                db, tenant_id, intent, was_correct_rate=rate, sample_count=samples,
                notes=f"canary ramp: {decision.reason}",
            )
            out.promotion_action = res.action
            out.applied = res.action == "rolled_back"
            if res.action == "error":
                out.action = ERROR
            return out

        if decision.action == PROMOTE_CHECK:
            res = await promotion_service.check_and_promote(db, tenant_id, intent)
            out.promotion_action = res.action
            out.applied = res.action in ("promoted", "rolled_back", "held_for_generalization")
            if res.action == "error":
                out.action = ERROR
            return out

        # ADVANCE
        sql = _ADVANCE_TO_FINAL if is_final(decision.next_pct) else _ADVANCE
        result = await db.execute(text(sql), {
            "tenant_id": tenant_id, "intent": intent,
            "cluster_id": row.candidate_cluster_id,
            "old_pct": pct, "next_pct": decision.next_pct,
        })
        if (result.rowcount or 0) == 0:
            await self._safe_rollback(db)
            out.action = "skipped_changed_concurrently"
            out.reason = "canary row changed since it was read; nothing written"
            return out
        await db.commit()
        out.applied = True
        await self._log_ramp_event(
            db, tenant_id, intent, row.candidate_cluster_id, samples, rate,
            decision.next_pct, f"{pct:g}->{decision.next_pct:g}: {decision.reason}",
        )
        logger.info("CanaryRamp ADVANCED tenant=%s intent=%s %g%% -> %g%% (%s)",
                    tenant_id, intent, pct, decision.next_pct, decision.reason)
        return out

    async def _log_ramp_event(self, db, tenant_id, intent, cluster_id, samples, rate, pct, notes):
        try:
            await db.execute(text(_LOG_EVENT), {
                "tenant_id": tenant_id, "intent": intent, "cluster_id": cluster_id,
                "sample_count": samples, "was_correct_rate": rate,
                "canary_pct": pct, "notes": notes,
            })
            await db.commit()
        except Exception:
            # The ramp itself already committed; the audit row is best-effort.
            logger.error("CanaryRamp audit log failed tenant=%s intent=%s",
                         tenant_id, intent, exc_info=True)
            await self._safe_rollback(db)

    @staticmethod
    async def _safe_rollback(db) -> None:
        try:
            await db.rollback()
        except Exception:
            pass


canary_ramp_service = CanaryRampService()


# ── Runner (all tenants, lock-guarded) + CLI ─────────────────────────

async def run_all(
    *,
    tenant_id: int | None = None,
    dry_run: bool = False,
    min_dwell_hours: float = MIN_STAGE_DWELL_HOURS,
    session_factory=None,
    lock=None,
) -> dict:
    """One pass over every active tenant that has a canary. Returns a
    JSON-able summary; `skipped` is True when another run holds the lock."""
    if session_factory is None:
        from app.core.database import AsyncSessionLocal as session_factory
    lock = lock or advisory_job_lock

    async with lock(LOCK_NAME) as acquired:
        if not acquired:
            logger.info("CanaryRamp: another run holds the lock -- skipping")
            return {"skipped": True, "dryRun": dry_run, "errors": 0, "tenants": []}

        async with session_factory() as db:
            if tenant_id is None:
                rows = await db.execute(text(_TENANTS_WITH_CANARIES))
            else:
                rows = await db.execute(text(_TENANT_ONE_WITH_CANARIES), {"tenant_id": tenant_id})
            tenant_ids = [r.tenant_id for r in rows]

        results: list[RampRunResult] = []
        for tid in tenant_ids:
            async with session_factory() as db:  # fresh session per tenant
                results.append(await canary_ramp_service.run_for_tenant(
                    db, tid, dry_run=dry_run, min_dwell_hours=min_dwell_hours))

    return {
        "skipped": False,
        "dryRun": dry_run,
        "errors": sum(r.errors for r in results),
        "tenants": [r.to_dict() for r in results],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Step active canaries 5->20->50->100 while accuracy holds.")
    ap.add_argument("--tenant", type=int, help="only this tenant id")
    ap.add_argument("--dry-run", action="store_true", help="evaluate and print; write nothing")
    ap.add_argument("--min-dwell-hours", type=float, default=MIN_STAGE_DWELL_HOURS)
    ap.add_argument("--loop", action="store_true", help="repeat forever (Ctrl-C / container stop to end)")
    ap.add_argument("--interval-minutes", type=float, default=60.0, help="with --loop")
    args = ap.parse_args(argv)

    async def once() -> int:
        summary = await run_all(
            tenant_id=args.tenant, dry_run=args.dry_run, min_dwell_hours=args.min_dwell_hours,
        )
        print(json.dumps(summary, indent=2, default=str))
        return 1 if summary["errors"] else 0

    async def loop() -> int:
        worst = 0
        while True:
            try:
                worst = max(worst, await once())
            except Exception:
                logger.error("CanaryRamp loop iteration failed", exc_info=True)
                worst = 1
            await asyncio.sleep(args.interval_minutes * 60)

    return asyncio.run(loop() if args.loop else once())


if __name__ == "__main__":
    raise SystemExit(main())
