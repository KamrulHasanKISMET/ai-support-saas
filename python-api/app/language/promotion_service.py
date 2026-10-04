"""
PROMOTION SERVICE (Phase 3 — Automated Learning/Promotion Pipeline)

docs/LANGUAGE_INTELLIGENCE.md Phase 3, step 2: govern the
shadow-eval → canary → promote/rollback lifecycle for candidate
intent clusters.

What this IS:
    Two public methods:
      start_canary(db, tenant_id, intent, candidate_cluster_id, pct=5.0)
          Activates a canary split: writes to canary_splits so
          RoutingService begins routing pct% of brain-eligible traffic
          for this intent to the candidate cluster. Logs 'canary_start'.

      check_and_promote(db, tenant_id, intent)
          Reads routing_decisions rows from the canary period and
          measures was_correct_rate for the candidate cluster. If:
            >= MIN_PROMOTE_ACCURACY and >= MIN_CANARY_SAMPLES:
                → generalization gate (see below). If it passes (or is
                  not applicable): promote (is_promoted=TRUE, retire
                  old cluster, clear canary_splits, log 'canary_pass').
                  If it fails: hold_for_generalization (retire
                  candidate WITHOUT promoting, clear canary_splits,
                  log 'generalization_fail'). If it cannot be run this
                  cycle: pending, retried next time.
            < MIN_ROLLBACK_ACCURACY (and >= MIN_CANARY_SAMPLES):
                → rollback (clear candidate, clear canary_splits, log
                  'canary_fail')
            else (not enough data yet):
                → pending (no action, log nothing)

Generalization gate (docs/GENERAL_LANGUAGE_BRAIN.md §5.5,
app/language/generalization_eval.py): was_correct_rate above measures
accuracy on whatever live canary traffic showed up -- it says nothing
about whether the candidate learned the underlying GOAL or just
memorized the handful of examples it was built from. Before a
promotion that has already cleared the accuracy bar, _run_generalization_gate()
additionally runs generalization_eval against the tenant's real
cluster embeddings, scored on that intent's HELD-OUT multilingual
cases (cases the candidate was never built from). Three outcomes:
    "pass" / "not_applicable" → proceed to promote. "not_applicable"
        means the intent has no held-out cases yet (eval coverage is
        added intent-by-intent); absence of a test is not a failure.
    "fail" → the candidate looks accurate on canary traffic but does
        not generalize (e.g. very language-skewed accuracy, or low
        cross-language concept consistency). Treated like a rollback
        -- the candidate is retired -- but logged distinctly
        ('generalization_fail') so operators can tell "wrong" apart
        from "overfit."
    "error" → the eval itself could not run (embedding/LLM provider
        unavailable, DB error). Fails CLOSED for promotion (does not
        promote blind) but does NOT roll back either -- an inability
        to check is not evidence of overfitting. Held at 'pending',
        retried on the next check_and_promote call.

What this is NOT:
    - Not ClusterBuilderService (which builds candidates from
      language_experiences). PromotionService only acts on candidate
      clusters that ClusterBuilderService already created.
    - Not called from the hot request path. Called out-of-band.
    - Not responsible for routing during the canary. RoutingService
      reads canary_splits and handles canary traffic allocation.
    - Not the generalization eval itself (app/language/generalization_eval.py
      owns the measurement; PromotionService only consumes its verdict).

Accuracy thresholds (class-level constants):

    MIN_PROMOTE_ACCURACY = 0.95
        was_correct_rate required for promotion. Higher than
        CalibrationService's MIN_AGREEMENT_RATE (0.90) because
        here we are measuring live routing accuracy (was_correct in
        routing_decisions), not shadow agreement. A promoted cluster
        directly affects customer-facing responses.

    MIN_ROLLBACK_ACCURACY = 0.85
        was_correct_rate below which the canary is immediately rolled
        back. Between 0.85 and 0.95, the canary continues accumulating
        data (pending). This gap prevents thrashing on a slow data day.

    MIN_CANARY_SAMPLES = 20
        Minimum routing_decisions rows (routed_to='brain') for the
        candidate cluster before any promote/rollback decision is made.
        Below this, result is 'pending' regardless of accuracy.
"""

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.language.cluster_builder_types import PromotionCheckResult
from app.language.generalization_eval import load_eval_set, passes_gate
from app.language.generalization_eval import run_for_tenant as run_generalization_eval
from app.language.tenant_calibration_config import TenantCalibrationConfig

MIN_PROMOTE_ACCURACY: float = 0.95
MIN_ROLLBACK_ACCURACY: float = 0.85
MIN_CANARY_SAMPLES: int = 20


@dataclass
class _GateOutcome:
    """
    Internal result of _run_generalization_gate(). Not returned to
    callers of check_and_promote() -- folded into PromotionCheckResult
    (action='held_for_generalization' | 'promoted' | 'pending').

    status:
        "pass"           -- eval ran, passes_gate() said yes
        "fail"           -- eval ran, passes_gate() said no (reasons filled)
        "not_applicable" -- intent has no held-out eval cases; skipped
        "error"          -- eval could not run at all (reasons filled)
    """
    status: str
    reasons: list[str] = field(default_factory=list)


class PromotionService:
    """
    Governs canary traffic → promote/rollback lifecycle.
    One module-level singleton (promotion_service below). Stateless.
    """

    async def start_canary(
        self,
        db: AsyncSession,
        tenant_id: int,
        intent: str,
        candidate_cluster_id: int,
        canary_pct: float = 5.0,
    ) -> None:
        """
        Activate a canary split for (tenant_id, intent).
        Upserts into canary_splits; logs 'canary_start'.
        Replaces any existing canary for this intent.
        """
        try:
            # Upsert canary_splits row
            await db.execute(
                text("""
                    INSERT INTO canary_splits (
                        tenant_id, intent, candidate_cluster_id, canary_pct, started_at, updated_at
                    ) VALUES (
                        :tenant_id, :intent, :cluster_id, :pct, NOW(), NOW()
                    )
                    ON CONFLICT (tenant_id, intent) DO UPDATE
                        SET candidate_cluster_id = EXCLUDED.candidate_cluster_id,
                            canary_pct           = EXCLUDED.canary_pct,
                            updated_at           = NOW()
                """),
                {"tenant_id": tenant_id, "intent": intent,
                 "cluster_id": candidate_cluster_id, "pct": canary_pct},
            )
            await db.commit()

            # Mark the candidate cluster as is_candidate=TRUE
            await db.execute(
                text("""
                    UPDATE intent_clusters
                       SET is_candidate = TRUE
                     WHERE id = :cluster_id AND tenant_id = :tenant_id
                """),
                {"cluster_id": candidate_cluster_id, "tenant_id": tenant_id},
            )
            await db.commit()

            await self._log_event(
                db, tenant_id=tenant_id, intent=intent,
                cluster_id=candidate_cluster_id, version=None,
                event_type="canary_start", canary_pct=canary_pct,
            )
            logger.info(
                "PromotionService canary started tenant=%s intent=%s cluster=%s pct=%.1f%%",
                tenant_id, intent, candidate_cluster_id, canary_pct,
            )
        except Exception:
            logger.error(
                "PromotionService.start_canary failed tenant=%s intent=%s",
                tenant_id, intent, exc_info=True,
            )

    async def check_and_promote(
        self,
        db: AsyncSession,
        tenant_id: int,
        intent: str,
    ) -> PromotionCheckResult:
        """
        Check canary accuracy and promote or rollback if thresholds are met.
        Returns PromotionCheckResult. Never raises.
        """
        try:
            return await self._check(db, tenant_id, intent)
        except Exception:
            logger.error(
                "PromotionService.check_and_promote failed tenant=%s intent=%s",
                tenant_id, intent, exc_info=True,
            )
            return PromotionCheckResult(intent=intent, action="error")

    async def abort_canary(
        self,
        db: AsyncSession,
        tenant_id: int,
        intent: str,
        *,
        was_correct_rate: float | None,
        sample_count: int | None,
        notes: str,
    ) -> PromotionCheckResult:
        """
        Retire the current candidate and clear its canary WITHOUT
        promoting -- the same DB effect as a rollback, logged as
        'canary_fail' with the caller's notes. Used by the canary ramp
        (canary_ramp_service.py) when a ramp stage regresses; it applies
        no accuracy thresholds itself (the caller decided). Never raises.
        """
        try:
            canary_row = await self._fetch_canary(db, tenant_id, intent)
            if canary_row is None:
                return PromotionCheckResult(intent=intent, action="no_canary")
            cluster_id = canary_row["candidate_cluster_id"]
            canary_pct = canary_row["canary_pct"]

            await self._retire_candidate_and_clear_canary(db, tenant_id, intent, cluster_id)
            await self._log_event(
                db, tenant_id=tenant_id, intent=intent,
                cluster_id=cluster_id, version=None,
                event_type="canary_fail", canary_pct=canary_pct,
                was_correct_rate=was_correct_rate, sample_count=sample_count,
                notes=notes,
            )
            logger.warning(
                "PromotionService ABORTED canary tenant=%s intent=%s cluster=%s "
                "pct=%.1f%% notes=%s", tenant_id, intent, cluster_id, canary_pct, notes,
            )
            return PromotionCheckResult(
                intent=intent, action="rolled_back",
                was_correct_rate=was_correct_rate, cluster_id=cluster_id, notes=notes,
            )
        except Exception:
            logger.error(
                "PromotionService.abort_canary failed tenant=%s intent=%s",
                tenant_id, intent, exc_info=True,
            )
            return PromotionCheckResult(intent=intent, action="error")

    # ── Internal ─────────────────────────────────────────────────────

    async def _check(
        self, db: AsyncSession, tenant_id: int, intent: str
    ) -> PromotionCheckResult:
        # Phase 4: load per-tenant thresholds
        tenant_cfg = await TenantCalibrationConfig.load(db, tenant_id)

        # 1. Is there an active canary for this intent?
        canary_row = await self._fetch_canary(db, tenant_id, intent)
        if canary_row is None:
            return PromotionCheckResult(intent=intent, action="no_canary")

        candidate_cluster_id = canary_row["candidate_cluster_id"]
        canary_pct = canary_row["canary_pct"]

        # 2. Measure was_correct_rate from routing_decisions for this candidate
        accuracy_row = await self._measure_canary_accuracy(
            db, tenant_id, intent, candidate_cluster_id
        )
        sample_count = accuracy_row["sample_count"]
        was_correct_rate = accuracy_row["was_correct_rate"]

        if sample_count < MIN_CANARY_SAMPLES:  # global min -- canary sample floor doesn't vary per-tenant
            logger.info(
                "PromotionService tenant=%s intent=%s canary pending: "
                "%d samples (need %d)",
                tenant_id, intent, sample_count, MIN_CANARY_SAMPLES,
            )
            return PromotionCheckResult(
                intent=intent, action="pending",
                was_correct_rate=was_correct_rate,
                cluster_id=candidate_cluster_id,
                notes=f"{sample_count}/{MIN_CANARY_SAMPLES} samples",
            )

        if was_correct_rate >= tenant_cfg.min_promote_accuracy:
            gate = await self._run_generalization_gate(db, tenant_id, intent)

            if gate.status == "fail":
                await self._hold_for_generalization(
                    db, tenant_id, intent, candidate_cluster_id, canary_pct,
                    was_correct_rate, sample_count, gate.reasons,
                )
                return PromotionCheckResult(
                    intent=intent, action="held_for_generalization",
                    was_correct_rate=was_correct_rate,
                    cluster_id=candidate_cluster_id,
                    notes="; ".join(gate.reasons) or "generalization gate failed",
                )

            if gate.status == "error":
                logger.warning(
                    "PromotionService tenant=%s intent=%s generalization gate "
                    "could not run this cycle -- holding at pending, will retry: %s",
                    tenant_id, intent, "; ".join(gate.reasons),
                )
                return PromotionCheckResult(
                    intent=intent, action="pending",
                    was_correct_rate=was_correct_rate,
                    cluster_id=candidate_cluster_id,
                    notes="generalization gate unavailable this cycle",
                )

            # gate.status in ("pass", "not_applicable") -> proceed
            await self._promote(db, tenant_id, intent, candidate_cluster_id, canary_pct, was_correct_rate, sample_count)
            return PromotionCheckResult(
                intent=intent, action="promoted",
                was_correct_rate=was_correct_rate,
                cluster_id=candidate_cluster_id,
            )

        if was_correct_rate < tenant_cfg.min_rollback_accuracy:
            await self._rollback(db, tenant_id, intent, candidate_cluster_id, canary_pct, was_correct_rate, sample_count)
            return PromotionCheckResult(
                intent=intent, action="rolled_back",
                was_correct_rate=was_correct_rate,
                cluster_id=candidate_cluster_id,
            )

        # Between thresholds → pending, keep accumulating
        logger.info(
            "PromotionService tenant=%s intent=%s canary pending: "
            "was_correct_rate=%.3f (need %.2f to promote, < %.2f to rollback)",
            tenant_id, intent, was_correct_rate, MIN_PROMOTE_ACCURACY, MIN_ROLLBACK_ACCURACY,
        )
        return PromotionCheckResult(
            intent=intent, action="pending",
            was_correct_rate=was_correct_rate,
            cluster_id=candidate_cluster_id,
            notes=f"rate={was_correct_rate:.3f} between thresholds",
        )

    async def _promote(
        self, db: AsyncSession, tenant_id: int, intent: str,
        candidate_cluster_id: int, canary_pct: float,
        was_correct_rate: float, sample_count: int,
    ) -> None:
        """Promote candidate → live, retire old promoted cluster, clear canary."""
        # Retire the currently promoted cluster (if any)
        await db.execute(
            text("""
                UPDATE intent_clusters
                   SET is_promoted = FALSE,
                       retired_at  = NOW()
                 WHERE tenant_id   = :tenant_id
                   AND intent      = :intent
                   AND is_promoted = TRUE
            """),
            {"tenant_id": tenant_id, "intent": intent},
        )
        # Promote the candidate
        await db.execute(
            text("""
                UPDATE intent_clusters
                   SET is_promoted  = TRUE,
                       is_candidate = FALSE,
                       promoted_at  = NOW(),
                       retired_at   = NULL
                 WHERE id          = :cluster_id
                   AND tenant_id   = :tenant_id
            """),
            {"cluster_id": candidate_cluster_id, "tenant_id": tenant_id},
        )
        # Clear the canary split
        await db.execute(
            text("DELETE FROM canary_splits WHERE tenant_id = :tid AND intent = :intent"),
            {"tid": tenant_id, "intent": intent},
        )
        await db.commit()

        await self._log_event(
            db, tenant_id=tenant_id, intent=intent,
            cluster_id=candidate_cluster_id, version=None,
            event_type="canary_pass", canary_pct=canary_pct,
            was_correct_rate=was_correct_rate, sample_count=sample_count,
        )
        logger.info(
            "PromotionService PROMOTED tenant=%s intent=%s cluster=%s "
            "was_correct_rate=%.3f samples=%d",
            tenant_id, intent, candidate_cluster_id, was_correct_rate, sample_count,
        )

    async def _rollback(
        self, db: AsyncSession, tenant_id: int, intent: str,
        candidate_cluster_id: int, canary_pct: float,
        was_correct_rate: float, sample_count: int,
    ) -> None:
        """Roll back the candidate, clear canary."""
        await self._retire_candidate_and_clear_canary(db, tenant_id, intent, candidate_cluster_id)

        await self._log_event(
            db, tenant_id=tenant_id, intent=intent,
            cluster_id=candidate_cluster_id, version=None,
            event_type="canary_fail", canary_pct=canary_pct,
            was_correct_rate=was_correct_rate, sample_count=sample_count,
        )
        logger.warning(
            "PromotionService ROLLED BACK tenant=%s intent=%s cluster=%s "
            "was_correct_rate=%.3f samples=%d",
            tenant_id, intent, candidate_cluster_id, was_correct_rate, sample_count,
        )

    async def _hold_for_generalization(
        self, db: AsyncSession, tenant_id: int, intent: str,
        candidate_cluster_id: int, canary_pct: float,
        was_correct_rate: float, sample_count: int, reasons: list[str],
    ) -> None:
        """
        Candidate cleared the accuracy bar but failed the §5.5
        generalization gate -- it looks accurate on the canary traffic
        it actually saw but does not generalize (e.g. skewed per-language
        accuracy, low cross-language concept consistency). Retired the
        same way a rollback is, but logged as 'generalization_fail' so
        this is distinguishable from a plain accuracy rollback.
        """
        await self._retire_candidate_and_clear_canary(db, tenant_id, intent, candidate_cluster_id)

        await self._log_event(
            db, tenant_id=tenant_id, intent=intent,
            cluster_id=candidate_cluster_id, version=None,
            event_type="generalization_fail", canary_pct=canary_pct,
            was_correct_rate=was_correct_rate, sample_count=sample_count,
            notes="; ".join(reasons) or None,
        )
        logger.warning(
            "PromotionService HELD FOR GENERALIZATION tenant=%s intent=%s cluster=%s "
            "was_correct_rate=%.3f samples=%d reasons=%s",
            tenant_id, intent, candidate_cluster_id, was_correct_rate, sample_count, reasons,
        )

    async def _retire_candidate_and_clear_canary(
        self, db: AsyncSession, tenant_id: int, intent: str, candidate_cluster_id: int,
    ) -> None:
        """Shared DB effect of _rollback() and _hold_for_generalization():
        retire the candidate without promoting it, clear its canary split."""
        await db.execute(
            text("""
                UPDATE intent_clusters
                   SET is_candidate = FALSE,
                       retired_at   = NOW()
                 WHERE id          = :cluster_id
                   AND tenant_id   = :tenant_id
            """),
            {"cluster_id": candidate_cluster_id, "tenant_id": tenant_id},
        )
        await db.execute(
            text("DELETE FROM canary_splits WHERE tenant_id = :tid AND intent = :intent"),
            {"tid": tenant_id, "intent": intent},
        )
        await db.commit()

    # ── Generalization gate ─────────────────────────────────────────

    async def _run_generalization_gate(
        self, db: AsyncSession, tenant_id: int, intent: str,
    ) -> _GateOutcome:
        """
        See the module docstring's "Generalization gate" section.
        Never raises -- any failure to load the eval set or run the
        eval is reported as status="error" so the caller can hold the
        promotion at 'pending' instead of promoting blind.
        """
        try:
            cases = load_eval_set()
        except Exception:
            logger.error(
                "PromotionService generalization gate: could not load eval set "
                "tenant=%s intent=%s", tenant_id, intent, exc_info=True,
            )
            return _GateOutcome(status="error", reasons=["eval set failed to load"])

        if not any(c.intent == intent for c in cases):
            # No held-out coverage for this intent yet -- not this
            # candidate's fault, and coverage is added intent-by-intent.
            return _GateOutcome(status="not_applicable")

        try:
            report = await run_generalization_eval(db, tenant_id, intent=intent)
        except Exception:
            logger.error(
                "PromotionService generalization gate raised tenant=%s intent=%s",
                tenant_id, intent, exc_info=True,
            )
            return _GateOutcome(status="error", reasons=["generalization eval raised"])

        passed, reasons = passes_gate(report)
        return _GateOutcome(status="pass" if passed else "fail", reasons=reasons)

    # ── DB helpers ───────────────────────────────────────────────────

    async def _fetch_canary(
        self, db: AsyncSession, tenant_id: int, intent: str
    ) -> dict | None:
        result = await db.execute(
            text("""
                SELECT candidate_cluster_id, canary_pct
                  FROM canary_splits
                 WHERE tenant_id = :tenant_id AND intent = :intent
                 LIMIT 1
            """),
            {"tenant_id": tenant_id, "intent": intent},
        )
        row = result.first()
        if row is None:
            return None
        return {"candidate_cluster_id": row.candidate_cluster_id, "canary_pct": float(row.canary_pct)}

    async def _measure_canary_accuracy(
        self, db: AsyncSession, tenant_id: int, intent: str, candidate_cluster_id: int
    ) -> dict:
        """
        Measure was_correct_rate from routing_decisions for the canary
        period. We use routing_decisions rows where:
          - routed_to = 'brain' (only brain routes have was_correct set)
          - predicted_intent = intent (this is the brain's prediction)
          - created_at >= when the canary started (canary_splits.started_at)
        """
        result = await db.execute(
            text("""
                SELECT COUNT(*) AS sample_count,
                       AVG(CASE WHEN rd.was_correct THEN 1.0 ELSE 0.0 END) AS was_correct_rate
                  FROM routing_decisions rd
                  JOIN canary_splits cs
                    ON cs.tenant_id = rd.tenant_id
                   AND cs.intent    = rd.predicted_intent
                 WHERE rd.tenant_id        = :tenant_id
                   AND rd.predicted_intent = :intent
                   AND rd.routed_to        = 'brain'
                   AND rd.was_correct      IS NOT NULL
                   AND rd.created_at       >= cs.started_at
            """),
            {"tenant_id": tenant_id, "intent": intent},
        )
        row = result.first()
        return {
            "sample_count": int(row.sample_count) if row and row.sample_count else 0,
            "was_correct_rate": float(row.was_correct_rate) if row and row.was_correct_rate is not None else 0.0,
        }

    async def _log_event(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        intent: str,
        cluster_id: int | None,
        version: str | None,
        event_type: str,
        sample_count: int | None = None,
        agreement_rate: float | None = None,
        was_correct_rate: float | None = None,
        canary_pct: float | None = None,
        notes: str | None = None,
    ) -> None:
        try:
            await db.execute(
                text("""
                    INSERT INTO cluster_promotion_log (
                        tenant_id, intent, cluster_id, version, event_type,
                        sample_count, agreement_rate, was_correct_rate, canary_pct, notes
                    ) VALUES (
                        :tenant_id, :intent, :cluster_id, :version, :event_type,
                        :sample_count, :agreement_rate, :was_correct_rate, :canary_pct, :notes
                    )
                """),
                {
                    "tenant_id": tenant_id, "intent": intent, "cluster_id": cluster_id,
                    "version": version, "event_type": event_type,
                    "sample_count": sample_count, "agreement_rate": agreement_rate,
                    "was_correct_rate": was_correct_rate, "canary_pct": canary_pct,
                    "notes": notes,
                },
            )
            await db.commit()
        except Exception:
            logger.error(
                "PromotionService._log_event failed tenant=%s intent=%s event=%s",
                tenant_id, intent, event_type, exc_info=True,
            )


promotion_service = PromotionService()
