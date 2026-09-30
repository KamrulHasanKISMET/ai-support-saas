-- =====================================================================
-- LANGUAGE EXPERIENCE STORE (Phase 0 — foundation, write-only)
--
-- One row per Language Engine call. This is the substrate the "own
-- Language Brain" (Global Language Intelligence architecture) will
-- eventually learn from. It does NOT itself add any language
-- understanding -- the system is still 100% LLM-driven for language
-- understanding right now (see app/language/language_engine.py). This
-- table only starts *recording* that, so nothing is lost while the
-- own Brain doesn't exist yet.
--
-- Same pattern/isolation as agent_run_traces
-- (005_agent_foundation.sql): written by
-- app/language/experience_service.py, called from CoreAgent.run()
-- after the Kernel returns, wrapped in an isolated try/except -- a
-- write failure here NEVER blocks or alters the customer-facing
-- reply.
--
-- What this is NOT (see docs/LANGUAGE.md and the Language
-- Intelligence design doc): not a query/analytics API (write-only,
-- same as agent_run_traces), not a trained model, not a promotion
-- pipeline. Those are later phases. This migration is Phase 0's
-- entire scope: "define the schema, start logging."
-- =====================================================================

CREATE TABLE language_experiences (
    id                      SERIAL PRIMARY KEY,
    experience_id           UUID NOT NULL,           -- one per Language Engine call

    -- Identifiers (must always travel together -- multi-tenant boundary
    -- is enforced by these columns + every query filtering on tenant_id,
    -- not by application-level discipline alone)
    tenant_id               INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id             INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    conversation_id         INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id              INTEGER,                 -- node-api's message id; nullable, not FK'd
                                                      -- (same reasoning as agent_run_traces.message_id)
    request_id              VARCHAR(100),
    channel                 VARCHAR(50),

    -- Raw + semantic interpretation (Language Engine's actual output today)
    original_message        TEXT,                    -- NOTE: stored as-is for now -- no redaction/
                                                       -- hashing pipeline exists yet. Tenant-level
                                                       -- retention policy is a documented Phase 1+ gap,
                                                       -- not solved by this migration.
    detected_language        VARCHAR(20),
    reply_language            VARCHAR(20),
    normalized_message        TEXT,
    communication_style       VARCHAR(20),
    is_ambiguous               BOOLEAN NOT NULL DEFAULT FALSE,
    ambiguity_reason           TEXT,
    entity_spans                JSONB NOT NULL DEFAULT '[]',

    -- Brain vs. LLM bookkeeping (architecture doc sections 3/10/14).
    -- brain_used/brain_prediction/brain_version are always FALSE/NULL
    -- today -- there is no own Language Brain yet, only this table
    -- that will eventually feed one. llm_called is always TRUE today
    -- (100% of traffic is LLM-driven) -- this column existing now,
    -- always TRUE, is what makes "own-Brain coverage grows over time"
    -- a measurable claim later instead of an assumed one.
    brain_used                  BOOLEAN NOT NULL DEFAULT FALSE,
    brain_prediction            JSONB,
    brain_version                VARCHAR(50),
    llm_called                   BOOLEAN NOT NULL DEFAULT TRUE,
    llm_raw_confidence           DECIMAL(4,3),          -- Language Engine's OWN self-reported
                                                         -- confidence -- UNCALIBRATED. This is raw
                                                         -- model output, not the evidence-based,
                                                         -- calibrated confidence the design doc
                                                         -- describes -- that calibration engine is
                                                         -- Phase 2, not built by this migration.
    llm_model_version             VARCHAR(100),

    -- Outcome / learning gate. Nothing here is auto-trusted as
    -- permanent knowledge -- verification_result stays 'unverified'
    -- until a real verification step exists (Phase 3). learning_eligible
    -- is a coarse, honest first cut: FALSE for business-risk intents
    -- (order creation/status) per the design doc's "what should NEVER
    -- be learned automatically" guidance -- everything else defaults
    -- TRUE, but nothing actually consumes this flag to learn anything
    -- yet (no promotion pipeline exists -- Phase 3).
    final_intent                   VARCHAR(100),
    verification_result             VARCHAR(20) NOT NULL DEFAULT 'unverified',
    learning_eligible                BOOLEAN NOT NULL DEFAULT TRUE,

    created_at                        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_language_experiences_tenant ON language_experiences(tenant_id);
CREATE INDEX idx_language_experiences_tenant_created ON language_experiences(tenant_id, created_at);
CREATE INDEX idx_language_experiences_tenant_learning ON language_experiences(tenant_id, learning_eligible);
CREATE INDEX idx_language_experiences_experience_id ON language_experiences(experience_id);
