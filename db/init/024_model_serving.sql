-- =====================================================================
-- MODEL SERVED TURNS -- Phase 6 P6-4 (docs/PHASE_5_8_PLAN.md)
--
-- One row per turn where the serving hook (app/language/model_serving.py)
-- decided an own model (status 'active', or 'canary' only when the
-- separate canary flag is on) may supply the intent for that turn.
-- The hook is OFF by default (settings.model_serving_enabled).
--
-- HONESTY RULE (why `agrees` can be NULL): when the model's intent is
-- injected as the Kernel's hint, the Kernel's final intent IS the hint,
-- so "final == model" would always be true and prove nothing. Real
-- accuracy evidence therefore comes ONLY from `audited = TRUE` rows: for
-- a small sample of model-served turns the LLM Intent Engine still runs
-- (no hint is injected) and `agrees` records final_intent == model intent.
-- `agrees` is NULL for every non-audited row and must never be inferred.
--
-- PRIVACY: metadata only -- NO message text, NO person fields (§5.3).
-- 90-day retention (expires_at), purged by
-- `python -m app.language.model_serving --purge`.
-- Existing databases: apply with scripts/migrate.sh 024 (idempotent).
-- =====================================================================

CREATE TABLE IF NOT EXISTS model_served_turns (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    conversation_id   INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    experience_id     UUID,                        -- soft ref to language_experiences
    model_version     VARCHAR(60) NOT NULL,        -- language_models.version (soft ref)
    model_status      VARCHAR(12) NOT NULL,        -- 'active' | 'canary' at decision time
    canary_pct        FLOAT,                       -- NULL for active
    model_intent      VARCHAR(50) NOT NULL,        -- what the model said
    confidence        FLOAT NOT NULL,              -- temperature-calibrated
    hint_used         BOOLEAN NOT NULL,            -- Kernel actually skipped the LLM intent call
    audited           BOOLEAN NOT NULL DEFAULT FALSE,
    final_intent      VARCHAR(50),                 -- what the Kernel ended with
    agrees            BOOLEAN,                     -- ONLY set when audited
    automation_eligible BOOLEAN NOT NULL,          -- control_plane flag at decision time
    language          VARCHAR(30) NOT NULL DEFAULT 'und',
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at        TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP + INTERVAL '90 days'),
    CONSTRAINT model_served_agrees_only_when_audited
        CHECK (agrees IS NULL OR audited = TRUE)
);

CREATE INDEX IF NOT EXISTS idx_model_served_tenant_version
    ON model_served_turns (tenant_id, model_version, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_model_served_expires
    ON model_served_turns (expires_at);
