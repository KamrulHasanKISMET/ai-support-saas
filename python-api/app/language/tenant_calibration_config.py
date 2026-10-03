"""
TENANT CALIBRATION CONFIG (Phase 4 — Scale/Optimize)

Loads per-tenant overrides for calibration and routing thresholds.
When a tenant row exists in `tenant_calibration_config`, its values
replace the module-level constants in CalibrationService,
PromotionService, and NoveltyDetector. When no row exists, the
defaults (matching the module-level constants) are returned -- so
Phase 0-3 behavior is unchanged for tenants that have not opted in.

Pattern: one async loader + a frozen dataclass. No singleton cache
-- config is loaded per-run by each service that needs it, same as
AgentConfig. The table is tiny (one row per tenant) and the query
is a primary-key lookup, so the cost is negligible.

Usage (in CalibrationService.run_for_tenant):
    config = await TenantCalibrationConfig.load(db, tenant_id)
    # then use config.min_agreement_rate instead of MIN_AGREEMENT_RATE
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger


@dataclass(frozen=True)
class TenantCalibrationConfig:
    """
    Per-tenant overrides for calibration and routing thresholds.
    All fields match the columns in tenant_calibration_config exactly.
    Frozen so callers can't accidentally mutate a shared instance.
    """
    tenant_id: int

    # Calibration
    min_agreement_rate: float = 0.90
    min_sample_count: int = 30

    # Promotion
    min_promote_accuracy: float = 0.95
    min_rollback_accuracy: float = 0.85
    initial_canary_pct: float = 5.0

    # Novelty detection
    novelty_threshold: float = 0.50
    novelty_logging_enabled: bool = True

    @classmethod
    async def load(cls, db: AsyncSession, tenant_id: int) -> "TenantCalibrationConfig":
        """
        Load config for one tenant. Returns defaults if no row exists.
        Never raises -- a DB failure returns defaults and logs an error.
        """
        try:
            result = await db.execute(
                text("""
                    SELECT min_agreement_rate, min_sample_count,
                           min_promote_accuracy, min_rollback_accuracy,
                           initial_canary_pct,
                           novelty_threshold, novelty_logging_enabled
                      FROM tenant_calibration_config
                     WHERE tenant_id = :tenant_id
                """),
                {"tenant_id": tenant_id},
            )
            row = result.first()
            if row is None:
                return cls(tenant_id=tenant_id)  # all defaults
            return cls(
                tenant_id=tenant_id,
                min_agreement_rate=float(row.min_agreement_rate),
                min_sample_count=int(row.min_sample_count),
                min_promote_accuracy=float(row.min_promote_accuracy),
                min_rollback_accuracy=float(row.min_rollback_accuracy),
                initial_canary_pct=float(row.initial_canary_pct),
                novelty_threshold=float(row.novelty_threshold),
                novelty_logging_enabled=bool(row.novelty_logging_enabled),
            )
        except Exception:
            logger.error(
                "TenantCalibrationConfig.load failed tenant=%s -- using defaults",
                tenant_id, exc_info=True,
            )
            return cls(tenant_id=tenant_id)

    async def save(self, db: AsyncSession) -> None:
        """
        Upsert this config into tenant_calibration_config.
        Used by admin routes or tests that set up per-tenant overrides.
        """
        await db.execute(
            text("""
                INSERT INTO tenant_calibration_config (
                    tenant_id, min_agreement_rate, min_sample_count,
                    min_promote_accuracy, min_rollback_accuracy,
                    initial_canary_pct, novelty_threshold,
                    novelty_logging_enabled, updated_at
                ) VALUES (
                    :tenant_id, :min_agreement_rate, :min_sample_count,
                    :min_promote_accuracy, :min_rollback_accuracy,
                    :initial_canary_pct, :novelty_threshold,
                    :novelty_logging_enabled, NOW()
                )
                ON CONFLICT (tenant_id) DO UPDATE
                    SET min_agreement_rate    = EXCLUDED.min_agreement_rate,
                        min_sample_count      = EXCLUDED.min_sample_count,
                        min_promote_accuracy  = EXCLUDED.min_promote_accuracy,
                        min_rollback_accuracy = EXCLUDED.min_rollback_accuracy,
                        initial_canary_pct    = EXCLUDED.initial_canary_pct,
                        novelty_threshold     = EXCLUDED.novelty_threshold,
                        novelty_logging_enabled = EXCLUDED.novelty_logging_enabled,
                        updated_at            = NOW()
            """),
            {
                "tenant_id": self.tenant_id,
                "min_agreement_rate": self.min_agreement_rate,
                "min_sample_count": self.min_sample_count,
                "min_promote_accuracy": self.min_promote_accuracy,
                "min_rollback_accuracy": self.min_rollback_accuracy,
                "initial_canary_pct": self.initial_canary_pct,
                "novelty_threshold": self.novelty_threshold,
                "novelty_logging_enabled": self.novelty_logging_enabled,
            },
        )
        await db.commit()
