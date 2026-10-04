"""
CALIBRATION SERVICE (Phase 2 — Confidence Calibration + Live Routing)

docs/LANGUAGE_INTELLIGENCE.md Phase 2, step 1: turn the shadow Brain's
raw cosine similarity score into an evidence-based, calibrated
confidence before anything routes on it.

What this IS:
    Reads language_experiences rows where brain_used=TRUE (i.e., Phase
    1's shadow brain made a prediction) and, for each (tenant_id,
    intent) pair, finds the minimum cosine similarity threshold at
    which the observed agreement rate meets or exceeds
    MIN_AGREEMENT_RATE. That threshold is written back to the matching
    intent_clusters rows.

    RoutingService (app/language/routing_service.py) then reads
    calibrated_threshold from intent_clusters to decide whether to
    route a turn to the brain or the LLM.

What this is NOT:
    - Not a training loop. No model weights change.
    - Not called from the Kernel or from the hot request path. It is
      meant to be called out-of-band (a scheduled task, a management
      API route, or a manual SQL trigger) after enough shadow data has
      accumulated.
    - Not the Phase 3 cluster-building pipeline (that promotes real
      language_experiences rows into new intent_clusters rows). That is
      a separate concern; this file only updates calibration columns on
      existing rows.

Tenant isolation: every query here filters by tenant_id. The service
MUST be called once per tenant that has accumulated shadow data --
there is no cross-tenant computation.

Error handling: run_for_tenant() wraps its own DB work in a try/except
and returns a CalibrationResult regardless. A failure here never
affects live traffic (calibration runs out-of-band). Individual-intent
failures are logged and skipped so one bad intent doesn't block the
rest.

Parameters (class-level constants, not constructor args -- a
deliberate choice: these are system-wide policy, not per-call
configuration. Changing them is a reviewed, code-level decision):

    MIN_SAMPLE_COUNT = 30
        Minimum number of brain_used=TRUE rows required per intent
        before any threshold is written. Below this, agreement_rate
        is statistically unreliable (a 20/20 streak on 20 samples is
        not the same as 400/440 on 440 samples). Clusters below this
        threshold stay NULL (shadow-only) even if their raw agreement
        looks perfect.

    MIN_AGREEMENT_RATE = 0.90
        The agreement rate that a threshold must achieve to be
        considered "calibrated". 90% means at most 1 in 10 turns
        routed to the brain would have the wrong intent. Lower = more
        brain coverage but more errors; higher = safer but less
        coverage. The LANGUAGE_INTELLIGENCE.md design doc calls for a
        conservative start -- 90% is that start.

    THRESHOLD_SCAN_STEP = 0.05
        Resolution of the threshold scan. We try 0.50, 0.55, 0.60,
        ..., 0.95 and pick the lowest that meets MIN_AGREEMENT_RATE
        (more coverage at lower thresholds). Finer steps give better
        precision but require more samples to be statistically
        meaningful.
"""

import json
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.language.calibration_types import CalibrationResult, IntentCalibrationStats
from app.language.tenant_calibration_config import TenantCalibrationConfig

# ── Policy constants (system-wide; reviewed change required to alter) ──

MIN_SAMPLE_COUNT: int = 30
"""Minimum brain_used=TRUE rows per intent before writing a threshold."""

MIN_AGREEMENT_RATE: float = 0.90
"""Required agreement rate (brain == LLM) for a threshold to be used."""

THRESHOLD_SCAN_STEP: float = 0.05
"""Step size for the similarity threshold scan (0.50 → 0.95)."""

THRESHOLD_SCAN_MIN: float = 0.50
"""Lowest similarity threshold we consider for live routing."""

THRESHOLD_SCAN_MAX: float = 0.95
"""Highest similarity threshold we scan up to (inclusive)."""


class CalibrationService:
    """
    Reads shadow-brain data from language_experiences and updates
    calibrated_threshold / agreement_rate on intent_clusters rows.

    One instance is created at module level (calibration_service
    singleton below). Stateless between calls -- all state lives in the
    DB. Safe to call concurrently for different tenants; avoid
    concurrent calls for the same tenant (last-write-wins on the
    cluster rows -- no distributed lock exists yet).
    """

    async def run_for_tenant(
        self,
        db: AsyncSession,
        tenant_id: int,
    ) -> CalibrationResult:
        """
        Calibrate all intents for one tenant. Reads language_experiences
        (brain_used=TRUE), computes per-intent thresholds, writes back
        to intent_clusters, and logs a calibration_runs row.

        Returns a CalibrationResult summary. Never raises -- failures
        are logged and surfaced via the result's per_intent list where
        possible, or via a top-level log + empty result on catastrophic
        failure.
        """
        run_start = datetime.utcnow()
        try:
            return await self._run_calibration(db, tenant_id, run_start)
        except Exception:
            logger.error(
                "CalibrationService.run_for_tenant failed fatally tenant=%s",
                tenant_id,
                exc_info=True,
            )
            return CalibrationResult(
                tenant_id=tenant_id,
                intents_updated=0,
                total_samples=0,
                per_intent=[],
            )

    # ── Internal implementation ──────────────────────────────────────

    async def _run_calibration(
        self,
        db: AsyncSession,
        tenant_id: int,
        run_start: datetime,
    ) -> CalibrationResult:
        # Phase 4: load per-tenant config overrides (falls back to
        # module-level defaults when no row exists in
        # tenant_calibration_config, so Phase 0-3 behavior is unchanged).
        tenant_cfg = await TenantCalibrationConfig.load(db, tenant_id)

        # Step 1: fetch all shadow-brain rows for this tenant.
        rows = await self._fetch_shadow_rows(db, tenant_id)
        if not rows:
            logger.info(
                "CalibrationService tenant=%s no brain_used=TRUE rows yet -- nothing to calibrate",
                tenant_id,
            )
            return CalibrationResult(
                tenant_id=tenant_id,
                intents_updated=0,
                total_samples=0,
                per_intent=[],
            )

        total_samples = len(rows)

        # Step 2: group by final_intent and compute per-intent stats.
        per_intent_rows: dict[str, list[dict]] = {}
        for row in rows:
            intent = row["final_intent"]
            if intent is None:
                continue  # no resolved intent for this turn -- skip
            per_intent_rows.setdefault(intent, []).append(row)

        per_intent_stats: list[IntentCalibrationStats] = []
        intents_updated = 0

        for intent, intent_rows in per_intent_rows.items():
            try:
                stats = self._compute_stats_for_intent(
                        intent, intent_rows,
                        min_agreement_rate=tenant_cfg.min_agreement_rate,
                        min_sample_count=tenant_cfg.min_sample_count,
                    )
                per_intent_stats.append(stats)

                updated = await self._update_clusters(db, tenant_id, stats)
                if updated:
                    intents_updated += 1

            except Exception:
                logger.error(
                    "CalibrationService failed for intent=%s tenant=%s -- skipping",
                    intent,
                    tenant_id,
                    exc_info=True,
                )

        # Step 3: log a calibration_runs row.
        try:
            await self._record_calibration_run(
                db,
                tenant_id=tenant_id,
                intents_updated=intents_updated,
                total_samples=total_samples,
                min_agreement_rate=tenant_cfg.min_agreement_rate,
                min_sample_count=tenant_cfg.min_sample_count,
            )
        except Exception:
            logger.error(
                "CalibrationService failed to record calibration_run tenant=%s",
                tenant_id,
                exc_info=True,
            )

        result = CalibrationResult(
            tenant_id=tenant_id,
            intents_updated=intents_updated,
            total_samples=total_samples,
            per_intent=per_intent_stats,
        )

        logger.info(
            "CalibrationService.run_for_tenant done tenant=%s "
            "total_samples=%d intents_updated=%d",
            tenant_id,
            total_samples,
            intents_updated,
        )
        for stats in per_intent_stats:
            logger.info(
                "  intent=%-25s samples=%3d threshold=%s agreement_rate=%s",
                stats.intent,
                stats.sample_count,
                f"{stats.similarity_threshold:.2f}" if stats.similarity_threshold else "None",
                f"{stats.agreement_rate:.3f}" if stats.agreement_rate else "None",
            )

        return result

    # ── Data fetch ───────────────────────────────────────────────────

    async def _fetch_shadow_rows(
        self,
        db: AsyncSession,
        tenant_id: int,
    ) -> list[dict]:
        """
        Fetch all language_experiences rows for this tenant where the
        shadow brain made a prediction (brain_used=TRUE). Returns a
        list of dicts with the columns needed for calibration:
            final_intent, brain_prediction (JSONB parsed to dict)
        """
        result = await db.execute(
            text(
                """
                SELECT final_intent,
                       brain_prediction
                  FROM language_experiences
                 WHERE tenant_id      = :tenant_id
                   AND brain_used     = TRUE
                   AND final_intent   IS NOT NULL
                   AND brain_prediction IS NOT NULL
                 ORDER BY created_at ASC
                """
            ),
            {"tenant_id": tenant_id},
        )
        rows = []
        for row in result:
            bp = row.brain_prediction
            # brain_prediction is JSONB; SQLAlchemy may return it as a
            # dict already (asyncpg) or as a string (psycopg2-style).
            if isinstance(bp, str):
                try:
                    bp = json.loads(bp)
                except (json.JSONDecodeError, TypeError):
                    continue
            if not isinstance(bp, dict):
                continue
            similarity = bp.get("similarity")
            agreement = bp.get("agreement")
            if similarity is None or agreement is None:
                continue
            rows.append(
                {
                    "final_intent": row.final_intent,
                    "similarity": float(similarity),
                    "agreement": bool(agreement),
                }
            )
        return rows

    # ── Calibration computation ──────────────────────────────────────

    def _compute_stats_for_intent(
        self,
        intent: str,
        rows: list[dict],
        *,
        min_agreement_rate: float = MIN_AGREEMENT_RATE,
        min_sample_count: int = MIN_SAMPLE_COUNT,
    ) -> IntentCalibrationStats:
        """
        For one intent, scan similarity thresholds and return the LOWEST
        threshold that meets min_agreement_rate with at least
        min_sample_count rows above it.

        Phase 4: accepts per-tenant overrides for min_agreement_rate and
        min_sample_count via keyword args. Defaults to module-level
        constants so existing tests and Phase 0-3 behavior are unchanged.
        """
        total = len(rows)
        best_threshold: float | None = None
        best_agreement: float | None = None

        steps = round((THRESHOLD_SCAN_MAX - THRESHOLD_SCAN_MIN) / THRESHOLD_SCAN_STEP)
        for i in range(steps + 1):
            threshold = round(THRESHOLD_SCAN_MIN + i * THRESHOLD_SCAN_STEP, 10)
            above = [r for r in rows if r["similarity"] >= threshold]
            if len(above) < min_sample_count:
                continue
            agreed = sum(1 for r in above if r["agreement"])
            rate = agreed / len(above)
            if rate >= min_agreement_rate:
                best_threshold = threshold
                best_agreement = rate
                break

        return IntentCalibrationStats(
            intent=intent,
            similarity_threshold=best_threshold,
            agreement_rate=best_agreement,
            sample_count=total,
        )

    # ── DB write-back ────────────────────────────────────────────────

    async def _update_clusters(
        self,
        db: AsyncSession,
        tenant_id: int,
        stats: IntentCalibrationStats,
    ) -> bool:
        """
        Write calibration results back to intent_clusters rows for this
        (tenant_id, intent). Updates ALL rows for this tenant + intent
        (typically the default_seed rows plus any later-promoted ones).

        Returns True if at least one row was updated, False otherwise
        (e.g. no intent_clusters rows exist for this intent yet).

        When stats.similarity_threshold is None (not enough data or
        never reached MIN_AGREEMENT_RATE), we explicitly NULL the
        calibration columns so stale calibration from a previous run
        doesn't persist after the data changed (e.g. a batch of bad
        predictions came in and dropped the agreement rate).
        """
        result = await db.execute(
            text(
                """
                UPDATE intent_clusters
                   SET calibrated_threshold = :threshold,
                       agreement_rate       = :agreement_rate,
                       sample_count         = :sample_count,
                       last_calibrated_at   = NOW()
                 WHERE tenant_id = :tenant_id
                   AND intent    = :intent
                RETURNING id
                """
            ),
            {
                "threshold": stats.similarity_threshold,
                "agreement_rate": stats.agreement_rate,
                "sample_count": stats.sample_count,
                "tenant_id": tenant_id,
                "intent": stats.intent,
            },
        )
        rows_updated = result.rowcount if result.rowcount is not None else 0
        if rows_updated > 0:
            await db.commit()
            logger.debug(
                "CalibrationService updated %d intent_clusters rows "
                "tenant=%s intent=%s threshold=%s agreement_rate=%s samples=%d",
                rows_updated,
                tenant_id,
                stats.intent,
                stats.similarity_threshold,
                stats.agreement_rate,
                stats.sample_count,
            )
        return rows_updated > 0

    # ── Audit log ────────────────────────────────────────────────────

    async def _record_calibration_run(
        self,
        db: AsyncSession,
        tenant_id: int,
        intents_updated: int,
        total_samples: int,
        min_agreement_rate: float = MIN_AGREEMENT_RATE,
        min_sample_count: int = MIN_SAMPLE_COUNT,
    ) -> None:
        """
        Insert one row into calibration_runs so the operator can see
        when calibration last ran and what it did. Failure here is
        logged but never re-raised -- the calibration data itself is
        already written to intent_clusters.
        """
        await db.execute(
            text(
                """
                INSERT INTO calibration_runs (
                    tenant_id, intents_updated, total_samples,
                    min_agreement_rate, min_sample_count
                ) VALUES (
                    :tenant_id, :intents_updated, :total_samples,
                    :min_agreement_rate, :min_sample_count
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "intents_updated": intents_updated,
                "total_samples": total_samples,
                "min_agreement_rate": min_agreement_rate,
                "min_sample_count": min_sample_count,
            },
        )
        await db.commit()


# Module-level singleton -- same pattern as shadow_brain.shadow_brain,
# intent_engine.intent_engine, etc. Stateless between calls; safe to
# import and use from core_agent.py or a management route.
calibration_service = CalibrationService()
