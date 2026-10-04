-- =====================================================================
-- MODEL SHADOW PREDICTIONS -- Phase 6 (docs/PHASE_5_8_PLAN.md P6-2)
--
-- One row per turn scored by an own-model version that is in status
-- 'shadow' (language_models, migration 020). The model NEVER answers
-- the customer; this table only records what it WOULD have said next to
-- what the served path (brain/LLM) actually decided, so real-traffic
-- evidence accumulates before any canary.
--
-- PRIVACY: metadata only -- NO message text, NO normalized text, NO
-- person fields (GENERAL_LANGUAGE_BRAIN.md §5.3). 90-day retention
-- (expires_at), purged by `python -m app.language.model_shadow --purge`.
-- Existing databases: apply with scripts/migrate.sh 021 (idempotent).
-- =====================================================================

CREATE TABLE IF NOT EXISTS model_shadow_predictions (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    conversation_id   INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    experience_id     UUID,                       -- soft ref to language_experiences
    model_version     VARCHAR(60) NOT NULL,       -- language_models.version (soft ref)
    predicted_intent  VARCHAR(50) NOT NULL,
    confidence        FLOAT NOT NULL,             -- temperature-calibrated
    served_intent     VARCHAR(50),                -- what the live path decided
    served_source     VARCHAR(20),                -- 'brain' | 'llm'
    agrees            BOOLEAN,                    -- NULL when served_intent unknown
    language          VARCHAR(30) NOT NULL DEFAULT 'und',
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at        TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP + INTERVAL '90 days')
);

CREATE INDEX IF NOT EXISTS idx_model_shadow_tenant_version
    ON model_shadow_predictions (tenant_id, model_version, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_model_shadow_expires
    ON model_shadow_predictions (expires_at);
