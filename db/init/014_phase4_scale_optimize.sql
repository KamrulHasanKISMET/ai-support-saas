-- =====================================================================
-- PHASE 4 — SCALE / OPTIMIZE
-- docs/LANGUAGE_INTELLIGENCE.md Phase 4
--
-- Three additions:
--
--   1. Per-tenant calibration config (tenant_calibration_config)
--      Currently CalibrationService uses module-level constants
--      (MIN_AGREEMENT_RATE=0.90, MIN_SAMPLE_COUNT=30) that apply
--      identically to every tenant. Phase 4 lets each tenant tune
--      these independently -- a high-stakes tenant keeps 0.95, a
--      high-volume low-risk tenant drops to 0.80 for more coverage.
--
--   2. Novelty detection log (novelty_events)
--      When shadow_brain.predict() returns a similarity below a
--      per-tenant novelty threshold, the message is "novel" --
--      it does not resemble any known cluster. Logging these lets
--      the operator discover gaps in the cluster set and seed new
--      intents. Written by NoveltyDetector (Phase 4, non-blocking).
--
--   3. Cost tracking per resolved conversation (conversation_costs)
--      Tracks input_tokens + output_tokens per conversation aggregated
--      across all turns, so cost-per-resolved-conversation can be
--      trended over time as the Brain takes over more traffic (brain
--      routes skip the Intent Engine LLM call, reducing token spend).
--
-- Tenant isolation: every table here requires tenant_id NOT NULL FK.
-- =====================================================================


-- ─── 1. Per-tenant calibration config ────────────────────────────────
-- One row per tenant. Absent = use CalibrationService module-level
-- defaults (MIN_AGREEMENT_RATE=0.90, MIN_SAMPLE_COUNT=30).
-- Managed by an admin route or manual UPDATE -- no UI yet in Phase 4.

CREATE TABLE IF NOT EXISTS tenant_calibration_config (
    tenant_id               INTEGER PRIMARY KEY
                                REFERENCES tenants(id) ON DELETE CASCADE,

    -- Calibration thresholds (mirror CalibrationService constants)
    min_agreement_rate      FLOAT NOT NULL DEFAULT 0.90,
        -- Minimum agreement rate for a threshold to be written.
        -- Range: 0.70 (aggressive, high coverage) – 0.99 (very conservative).
    min_sample_count        INTEGER NOT NULL DEFAULT 30,
        -- Minimum brain_used=TRUE rows required before calibrating.

    -- Routing
    min_promote_accuracy    FLOAT NOT NULL DEFAULT 0.95,
        -- PromotionService: promote candidate when was_correct_rate >= this.
    min_rollback_accuracy   FLOAT NOT NULL DEFAULT 0.85,
        -- PromotionService: rollback candidate when was_correct_rate < this.
    initial_canary_pct      FLOAT NOT NULL DEFAULT 5.0,
        -- Starting canary traffic % when start_canary() is called.

    -- Novelty detection
    novelty_threshold       FLOAT NOT NULL DEFAULT 0.50,
        -- Similarity below this → message is "novel" (no matching cluster).
        -- Logged to novelty_events. Default matches THRESHOLD_SCAN_MIN
        -- so any message that would never qualify for brain routing is
        -- also considered novel.
    novelty_logging_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        -- Set FALSE to disable novelty logging for this tenant (e.g.
        -- very high-volume tenant where novelty events are noisy).

    updated_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- ─── 2. Novelty events ───────────────────────────────────────────────
-- One row per turn where the shadow brain's best similarity was below
-- the tenant's novelty_threshold. These are the gaps in the cluster
-- set -- messages the brain has never seen anything like.
-- Written by NoveltyDetector.record() (non-blocking, isolated).

CREATE TABLE IF NOT EXISTS novelty_events (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id         INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    conversation_id     INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    experience_id       UUID,           -- soft FK to language_experiences.experience_id
    request_id          VARCHAR(100),

    normalized_message  TEXT NOT NULL,  -- the message that was novel
    best_similarity     FLOAT,          -- highest similarity found (could be NULL if no clusters)
    best_intent         VARCHAR(100),   -- closest intent even though below threshold
    novelty_threshold   FLOAT NOT NULL, -- snapshot of tenant's threshold at log time
    brain_version       VARCHAR(50),

    -- Manual triage fields (filled by an operator or a future triage UI)
    triaged             BOOLEAN NOT NULL DEFAULT FALSE,
    triage_result       VARCHAR(50),    -- 'new_intent' | 'existing_intent' | 'noise' | 'spam'
    triage_intent       VARCHAR(100),   -- if triage_result='existing_intent', which one
    triaged_at          TIMESTAMP,
    triaged_by          VARCHAR(100),   -- operator email/id

    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_novelty_events_tenant
    ON novelty_events(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_novelty_events_untriaged
    ON novelty_events(tenant_id, triaged, created_at DESC)
    WHERE triaged = FALSE;


-- ─── 3. Conversation costs ───────────────────────────────────────────
-- Aggregate token spend per conversation. Updated (upserted) after
-- every turn. Lets us trend cost-per-resolved-conversation as the
-- Brain displaces LLM Intent Engine calls over time.
--
-- intent_engine_calls_saved: incremented when a turn's agent_run_traces
-- row shows intentSource='brain' -- that turn saved one LLM call.
-- This is the primary efficiency metric for Phase 4.

CREATE TABLE IF NOT EXISTS conversation_costs (
    id                          SERIAL PRIMARY KEY,
    tenant_id                   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    conversation_id             INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    UNIQUE(tenant_id, conversation_id),

    total_input_tokens          INTEGER NOT NULL DEFAULT 0,
    total_output_tokens         INTEGER NOT NULL DEFAULT 0,
    turn_count                  INTEGER NOT NULL DEFAULT 0,
    intent_engine_calls_saved   INTEGER NOT NULL DEFAULT 0,
        -- Turns where intentSource='brain' (LLM Intent Engine was skipped).
    resolved                    BOOLEAN NOT NULL DEFAULT FALSE,
        -- TRUE when the conversation reached a terminal state
        -- (state_engine marks it done). Lets us compute
        -- cost-per-RESOLVED-conversation rather than all conversations.

    first_turn_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_turn_at                TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at                 TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_conversation_costs_tenant
    ON conversation_costs(tenant_id, last_turn_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_costs_resolved
    ON conversation_costs(tenant_id, resolved, resolved_at DESC)
    WHERE resolved = TRUE;
