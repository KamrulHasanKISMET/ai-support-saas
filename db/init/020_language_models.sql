-- =====================================================================
-- LANGUAGE MODELS REGISTRY -- Phase 6 (own model learning / distillation)
-- docs/GENERAL_LANGUAGE_BRAIN.md §6 stage 4, §7.1 Phase 6.
--
-- One row per trained own-model VERSION, per tenant (§4: no cross-tenant
-- training data, so no cross-tenant model in this phase).
--
-- Lifecycle (enforced in app/language/model_registry.py, mirrored by the
-- CHECK below):
--   trained -> shadow -> canary -> active -> retired
--   trained|shadow|canary -> rejected | retired
-- A model NEVER answers a customer because it exists here. Nothing on
-- the request path reads this table yet; 'active' is the state a future
-- serving step will consult, after shadow and canary evidence.
--
-- PRIVACY: `artifact` holds weights + label names only. No training
-- text, no person-related field. `dataset_fingerprint` is a hash of
-- experience ids + split names, not of text.
-- Existing databases: db/init only auto-runs on a fresh volume; apply
-- with scripts/migrate.sh (idempotent).
-- =====================================================================

CREATE TABLE IF NOT EXISTS language_models (
    id                   BIGSERIAL PRIMARY KEY,
    tenant_id            INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    capability           VARCHAR(20) NOT NULL DEFAULT 'intent',   -- GLB capability B
    version              VARCHAR(60) NOT NULL,
    kind                 VARCHAR(40) NOT NULL,                    -- e.g. softmax_regression_v1
    status               VARCHAR(12) NOT NULL DEFAULT 'trained',
    embedding_model      VARCHAR(100) NOT NULL,                   -- weights are only valid for this
    dim                  INTEGER NOT NULL,
    labels               JSONB NOT NULL,
    artifact             JSONB NOT NULL,                          -- IntentModel.to_dict()
    artifact_sha256      CHAR(64) NOT NULL,
    dataset_fingerprint  CHAR(64) NOT NULL,
    train_counts         JSONB NOT NULL,                          -- {"train":..,"val":..,"test":..}
    eval_report          JSONB NOT NULL,                          -- model_eval.report_to_dict()
    created_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status_changed_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status_reason        TEXT,
    CONSTRAINT language_models_status_chk CHECK (
        status IN ('trained', 'shadow', 'canary', 'active', 'retired', 'rejected')),
    CONSTRAINT language_models_version_uniq UNIQUE (tenant_id, capability, version)
);

-- At most ONE active model per tenant+capability (a second activation
-- must retire the first in the same transaction).
CREATE UNIQUE INDEX IF NOT EXISTS uq_language_models_one_active
    ON language_models (tenant_id, capability) WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_language_models_tenant_status
    ON language_models (tenant_id, capability, status);
