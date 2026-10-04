"""
MODEL CANARY SERVICE -- Phase 6 P6-3 (docs/PHASE_5_8_PLAN.md,
docs/OWN_LANGUAGE_MODEL_TRAINING_PLAN.md rung R1).

Ramps a `language_models` row that is in status 'canary' up a fixed
percentage ladder while its evidence holds, mirroring
`canary_ramp_service.py`'s decision logic exactly (`decide_ramp` is
IMPORTED from there, not re-implemented, so the two ramps can never
silently diverge in policy).

    docker compose exec -T python-api python -m app.language.model_canary_service --dry-run
    docker compose exec -T python-api python -m app.language.model_canary_service
    docker compose exec -T python-api python -m app.language.model_canary_service --loop --interval-minutes 60

WHAT THIS DOES NOT DO YET (read before relying on it): it does not make
any model actually answer a customer. `model_canary_state.canary_pct` is
a number in a table until a serving hook (P6-4, `model_serving.py`,
default OFF) consults it. Two consequences:

  1. EVIDENCE SOURCE. `canary_ramp_service` measures the accuracy of
     turns the CANDIDATE ACTUALLY SERVED. Before a serving hook exists,
     nothing is actually served by the model, so this service instead
     measures **shadow agreement** (`model_shadow_predictions.agrees`,
     i.e. would-the-model-have-matched-what-was-served) over the same
     stage window. This is a real, useful signal (§ model_shadow.py) but
     it is NOT the same claim as "the model, when serving, was correct"
     -- shadow agreement can only ever be as good as what was served
     (LLM/brain), and it cannot see the cases where the model would have
     been right and the served answer was wrong. **The day P6-4 ships
     and starts writing real served-and-verified accuracy for
     model-served turns, point `_STAGE_STATS_MODE` at that query
     instead** (a `served_by_model` style column on the verification
     path, analogous to `routing_decisions.served_branch` from
     migration 019) and delete this paragraph.
  2. Because of (1), advancing this ladder to 100% and calling
     PromotionService-equivalent activation is *not itself* wired here:
     reaching the final stage only logs `event_type='activated'` and
     calls `model_registry.set_status(..., target='active', ...)`,
     which is safe (still gated: only a `canary` model may activate, and
     activating still does not matter to a customer without P6-4).

Isolation: one tenant's failure never blocks another (per-tenant
try/except); overlapping runs are prevented with the same advisory-lock
helper canary_ramp_service uses, under a different lock name.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass

from app.core.logging import logger
from app.language import model_registry
from app.language.canary_ramp_service import (
    ABORT, ADVANCE, ERROR, FINAL_STAGE_PCT, HOLD_BETWEEN, HOLD_DWELL, HOLD_SAMPLES,
    MIN_STAGE_DWELL_HOURS, PROMOTE_CHECK, RampDecision, decide_ramp, is_final,
)
from app.language.model_shadow import MIN_LANG_SLICE_N, ready_for_canary, summarize
from app.language.scheduler_lock import advisory_job_lock
from app.language.tenant_calibration_config import TenantCalibrationConfig

LOCK_NAME = "model_canary_ramp"
DEFAULT_MIN_PROMOTE_ACCURACY = 0.95     # placeholder, see THRESHOLDS_CALIBRATED below
DEFAULT_MIN_ROLLBACK_ACCURACY = 0.85
THRESHOLDS_CALIBRATED = False


@dataclass
class ModelRampOutcome:
    version: str
    action: str
    from_pct: float | None = None
    to_pct: float | None = None
    stage_samples: int | None = None
    stage_rate: float | None = None
    stage_age_hours: float | None = None
    reason: str = ""
    applied: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ModelRampRunResult:
    tenant_id: int
    outcomes: list[ModelRampOutcome]

    @property
    def errors(self) -> int:
        return sum(1 for o in self.outcomes if o.action == ERROR)

    def to_dict(self) -> dict:
        return {"tenantId": self.tenant_id, "errors": self.errors,
                "outcomes": [o.to_dict() for o in self.outcomes]}


# ── SQL ───────────────────────────────────────────────────────────────

_LIST_CANARIES = """
    SELECT mcs.capability, mcs.version, mcs.canary_pct, mcs.updated_at,
           EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - mcs.updated_at)) / 3600.0 AS stage_age_hours
      FROM model_canary_state mcs
     WHERE mcs.tenant_id = :tenant_id
     ORDER BY mcs.capability
"""

_STAGE_ROWS = """
    SELECT confidence, agrees, language
      FROM model_shadow_predictions
     WHERE tenant_id = :tenant_id AND model_version = :version AND created_at >= :since
"""

_ADVANCE = """
    UPDATE model_canary_state
       SET canary_pct = :next_pct, updated_at = NOW(),
           started_at = CASE WHEN :restart THEN NOW() ELSE started_at END
     WHERE tenant_id = :tenant_id AND capability = :capability AND version = :version
       AND canary_pct = :old_pct
"""

_LOG_EVENT = """
    INSERT INTO model_promotion_log
        (tenant_id, capability, version, event_type, sample_count, evidence_rate, canary_pct, notes)
    VALUES (:tenant_id, :capability, :version, :event_type, :n, :rate, :pct, :notes)
"""


class ModelCanaryService:
    """Stateless; module-level singleton `model_canary_service` below."""

    async def run_for_tenant(self, db, tenant_id: int, *, dry_run: bool = False,
                              min_dwell_hours: float = MIN_STAGE_DWELL_HOURS) -> ModelRampRunResult:
        from sqlalchemy import text  # lazy
        cfg = await TenantCalibrationConfig.load(db, tenant_id)
        try:
            canaries = list(await db.execute(text(_LIST_CANARIES), {"tenant_id": tenant_id}))
        except Exception:
            logger.error("ModelCanary: listing failed tenant=%s", tenant_id, exc_info=True)
            return ModelRampRunResult(tenant_id, [ModelRampOutcome(version="*", action=ERROR,
                                                                    reason="could not list canaries")])
        outcomes = []
        for row in canaries:
            try:
                outcomes.append(await self._ramp_one(db, tenant_id, row, cfg, dry_run, min_dwell_hours))
            except Exception:
                logger.error("ModelCanary failed tenant=%s version=%s", tenant_id, row.version, exc_info=True)
                await self._safe_rollback(db)
                outcomes.append(ModelRampOutcome(version=row.version, action=ERROR,
                                                  from_pct=float(row.canary_pct), reason="unexpected error (see logs)"))
        return ModelRampRunResult(tenant_id, outcomes)

    async def _ramp_one(self, db, tenant_id: int, row, cfg, dry_run: bool, min_dwell_hours: float) -> ModelRampOutcome:
        from sqlalchemy import text  # lazy
        capability, version = row.capability, row.version
        pct = float(row.canary_pct)
        age = float(row.stage_age_hours) if row.stage_age_hours is not None else 0.0
        out = ModelRampOutcome(version=version, action=HOLD_SAMPLES, from_pct=pct, stage_age_hours=round(age, 2))

        result = await db.execute(text(_STAGE_ROWS), {
            "tenant_id": tenant_id, "version": version, "since": row.updated_at,
        })
        shadow_rows = [{"confidence": r.confidence, "agrees": r.agrees, "language": r.language} for r in result]
        summary = summarize(shadow_rows)
        stage_samples = summary["scored"]
        stage_rate = summary["agreement"]

        min_promote = getattr(cfg, "min_promote_accuracy", None) or DEFAULT_MIN_PROMOTE_ACCURACY
        min_rollback = getattr(cfg, "min_rollback_accuracy", None) or DEFAULT_MIN_ROLLBACK_ACCURACY
        decision: RampDecision = decide_ramp(
            pct=pct, stage_samples=stage_samples, stage_rate=stage_rate,
            stage_age_hours=age, min_promote_accuracy=min_promote,
            min_rollback_accuracy=min_rollback, min_dwell_hours=min_dwell_hours,
        )
        out.stage_samples, out.stage_rate, out.action, out.reason = stage_samples, stage_rate, decision.action, decision.reason

        # An extra, model-specific guard on top of the shared ladder logic:
        # even a "ready to advance" stage must also clear the per-language
        # floor (a language with too few shadow samples is not gated, one
        # that is bad IS) before we actually move the percentage.
        lang_block = [f for f in ready_for_canary(summary) if f.startswith("language_slice")]
        if decision.action == ADVANCE and lang_block:
            out.action, out.reason = "hold_language_slice", "; ".join(lang_block)
            return out

        if dry_run or decision.action in (HOLD_SAMPLES, HOLD_DWELL, HOLD_BETWEEN):
            return out

        if decision.action == ABORT:
            await model_registry.set_status(db, tenant_id=tenant_id, version=version, target="rejected",
                                             reason=decision.reason, capability=capability)
            await db.execute(text(_LOG_EVENT), {"tenant_id": tenant_id, "capability": capability, "version": version,
                                                 "event_type": "canary_fail", "n": stage_samples, "rate": stage_rate,
                                                 "pct": pct, "notes": decision.reason})
            await db.commit()
            out.applied = True
            return out

        if decision.action == ADVANCE:
            restart = is_final(decision.next_pct)
            res = await db.execute(text(_ADVANCE), {"tenant_id": tenant_id, "capability": capability,
                                                      "version": version, "next_pct": decision.next_pct,
                                                      "old_pct": pct, "restart": restart})
            if getattr(res, "rowcount", 1) == 0:
                out.reason = "stage already advanced by a concurrent run"
                await db.rollback()
                return out
            await db.execute(text(_LOG_EVENT), {"tenant_id": tenant_id, "capability": capability, "version": version,
                                                 "event_type": "canary_ramp", "n": stage_samples, "rate": stage_rate,
                                                 "pct": decision.next_pct, "notes": decision.reason})
            await db.commit()
            out.to_pct, out.applied = decision.next_pct, True
            return out

        if decision.action == PROMOTE_CHECK:
            # Gated exactly like a shadow-to-active move: eval must have
            # passed at 'trained'->'shadow' time (model_registry enforces
            # separately); here we only require canary evidence to be
            # good, which the ladder already confirmed above.
            await model_registry.set_status(db, tenant_id=tenant_id, version=version, target="active",
                                             reason=f"canary evidence: {decision.reason}", capability=capability)
            await db.execute(text(_LOG_EVENT), {"tenant_id": tenant_id, "capability": capability, "version": version,
                                                 "event_type": "activated", "n": stage_samples, "rate": stage_rate,
                                                 "pct": FINAL_STAGE_PCT, "notes": decision.reason})
            await db.commit()
            out.action, out.applied = "activated", True
            return out

        return out

    @staticmethod
    async def _safe_rollback(db) -> None:
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass


model_canary_service = ModelCanaryService()


async def start_canary(db, *, tenant_id: int, version: str, capability: str = "intent",
                        initial_pct: float = 5.0, reason: str = "shadow evidence sufficient") -> None:
    """Move a 'shadow' model to 'canary' and create its ramp row.
    Refuses (via model_registry) unless the transition shadow->canary is legal."""
    from sqlalchemy import text  # lazy
    await model_registry.set_status(db, tenant_id=tenant_id, version=version, target="canary",
                                     reason=reason, capability=capability)
    await db.execute(text("""
        INSERT INTO model_canary_state (tenant_id, capability, version, canary_pct)
        VALUES (:t, :c, :v, :pct)
        ON CONFLICT (tenant_id, capability) DO UPDATE
           SET version = EXCLUDED.version, canary_pct = EXCLUDED.canary_pct,
               started_at = NOW(), updated_at = NOW()
    """), {"t": tenant_id, "c": capability, "v": version, "pct": initial_pct})
    await db.execute(text(_LOG_EVENT), {"tenant_id": tenant_id, "capability": capability, "version": version,
                                         "event_type": "canary_start", "n": None, "rate": None,
                                         "pct": initial_pct, "notes": reason})
    await db.commit()


async def _main(args) -> int:  # pragma: no cover -- needs real DB
    from app.core.database import AsyncSessionLocal

    async with advisory_job_lock(LOCK_NAME) as acquired:
        if not acquired:
            print("skipped: another run holds the lock")
            return 0
        async with AsyncSessionLocal() as db:
            tenant_ids = [args.tenant] if args.tenant else []  # tenant is required for now (no registry of "all canary tenants" yet)
            errors = 0
            for t in tenant_ids:
                result = await model_canary_service.run_for_tenant(db, t, dry_run=args.dry_run)
                print(json.dumps(result.to_dict(), indent=2, default=str))
                errors += result.errors
    return 1 if errors else 0


if __name__ == "__main__":  # pragma: no cover
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval-minutes", type=int, default=60)
    a = ap.parse_args()
    if a.loop:  # pragma: no cover
        async def loop():
            while True:
                await _main(a)
                await asyncio.sleep(a.interval_minutes * 60)
        asyncio.run(loop())
    else:
        import sys
        sys.exit(asyncio.run(_main(a)))
