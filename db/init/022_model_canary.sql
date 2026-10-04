-- =====================================================================
-- MODEL CANARY STATE -- Phase 6 P6-3 (docs/PHASE_5_8_PLAN.md).
--
-- One row per tenant+capability model that has left 'shadow' for
-- 'canary' (language_models.status). Tracks what % of eligible turns
-- the canary model is allowed to actually SERVE, and since when the
-- current stage has been running (mirrors canary_splits for clusters,
-- db/init/013_canary.sql).
--
-- IMPORTANT (read before wiring a serving hook): this table only
-- controls a *percentage decision*. Nothing consults it yet -- until
-- app/language/model_serving.py's should_serve() is called from
-- routing_service (P6-4, still off by default), canary_pct has no
-- effect on any customer-facing reply. model_canary_service.py (this
-- migration's companion) ramps the percentage using SHADOW AGREEMENT
-- as its evidence (model_shadow_predictions) until a serving hook
-- exists to measure real served accuracy -- see that module's
-- docstring for exactly when to switch the evidence source.
--
-- Existing databases: apply with scripts/migrate.sh 022 (idempotent).
-- =====================================================================

CREATE TABLE IF NOT EXISTS model_canary_state (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    capability      VARCHAR(20) NOT NULL DEFAULT 'intent',
    version         VARCHAR(60) NOT NULL,          -- language_models.version (soft ref)
    canary_pct      FLOAT NOT NULL DEFAULT 5.0,
    started_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- reset on stage advance
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT model_canary_state_uniq UNIQUE (tenant_id, capability)
);

-- One canary log, reusing the shape of cluster_promotion_log so both
-- ramps can eventually be read by one report. cluster_id stays NULL
-- for model events; version identifies the model instead.
CREATE TABLE IF NOT EXISTS model_promotion_log (
    id               BIGSERIAL PRIMARY KEY,
    tenant_id        INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    capability       VARCHAR(20) NOT NULL DEFAULT 'intent',
    version          VARCHAR(60) NOT NULL,
    event_type       VARCHAR(30) NOT NULL,   -- 'canary_start' | 'canary_ramp' | 'canary_fail' | 'activated'
    sample_count     INTEGER,
    evidence_rate    FLOAT,
    canary_pct       FLOAT,
    notes            TEXT,
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_model_canary_tenant ON model_canary_state (tenant_id, capability);
CREATE INDEX IF NOT EXISTS idx_model_promotion_log_tenant ON model_promotion_log (tenant_id, capability, created_at DESC);
