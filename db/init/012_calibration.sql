-- =====================================================================
-- CALIBRATION STATE (Phase 2 — Confidence Calibration + Live Routing)
--
-- docs/LANGUAGE_INTELLIGENCE.md Phase 2, step 1: turn the shadow
-- Brain's raw cosine similarity score into an evidence-based,
-- calibrated confidence before anything routes on it.
--
-- What this migration adds:
--   1. Per-cluster calibration state columns on `intent_clusters`.
--      These are updated (not appended) as new language_experiences
--      rows accumulate, so they live on the cluster row itself rather
--      than in a separate table.
--   2. A routing_decisions table that records every Phase 2 routing
--      choice (brain vs llm) BEFORE the Kernel runs, so calibration
--      accuracy can be measured independently of the existing
--      language_experiences write path.
--   3. A calibration_runs table: one row per CalibrationService run,
--      so the service can be scheduled periodically and its own
--      accuracy tracked over time without touching language_experiences
--      or intent_clusters directly.
--
-- Tenant isolation: every table here requires tenant_id (NOT NULL,
-- FK'd) and every query MUST filter by tenant_id, same as every other
-- tenant-scoped table in this codebase.
--
-- Shadow-only constraint (Phase 2 ONLY relaxes this for high-
-- confidence traffic, never removes it entirely):
--   routing_decisions.routed_to = 'brain' is only safe when
--   calibrated_threshold IS NOT NULL and agreement_rate >= the
--   minimum required by RoutingService (see
--   app/language/routing_service.py's MIN_AGREEMENT_RATE constant).
--   The Kernel never reads intent_clusters -- routing lives in
--   core_agent.py exactly as it did in Phase 1.
-- =====================================================================


-- ─── 1. Calibration state on existing intent_clusters rows ────────────
-- Added as nullable columns (no DEFAULT that would silently claim
-- calibrated data exists where none does). A NULL calibrated_threshold
-- means "not yet calibrated -- treat as shadow-only", which is the
-- same thing Phase 1's ShadowBrain already does.

ALTER TABLE intent_clusters
    ADD COLUMN IF NOT EXISTS calibrated_threshold FLOAT,
        -- The minimum similarity score at which this cluster's
        -- agreement_rate meets MIN_AGREEMENT_RATE. Set and updated
        -- by CalibrationService; NULL means uncalibrated.
    ADD COLUMN IF NOT EXISTS agreement_rate       FLOAT,
        -- Observed agreement rate (brain_prediction.agreement = TRUE
        -- / total brain_used rows) at or above calibrated_threshold.
        -- NULL until first calibration run.
    ADD COLUMN IF NOT EXISTS sample_count         INTEGER NOT NULL DEFAULT 0,
        -- Total language_experiences rows (brain_used=TRUE) that
        -- contributed to the most recent calibration computation.
        -- 0 = no data yet, not "calibrated on zero samples".
    ADD COLUMN IF NOT EXISTS last_calibrated_at   TIMESTAMP;
        -- When CalibrationService last updated these columns for this
        -- cluster. NULL = never calibrated.

-- ─── 2. Routing decisions (Phase 2 write path) ───────────────────────
-- One row per turn where a routing decision was made (not just logged
-- post-hoc). Written by core_agent.py BEFORE calling the Kernel, so
-- the routing logic's decisions exist independently of whether the
-- Kernel run succeeds.
--
-- routed_to = 'brain' means the Intent Engine step was skipped and
-- the brain's predicted_intent was injected directly.
-- routed_to = 'llm' means the full Kernel ran as in Phase 1 (no
-- change to the customer-facing path).
-- routed_to = 'shadow' means Phase 1 behaviour: brain ran but its
-- prediction was logged-only, not used.

CREATE TABLE IF NOT EXISTS routing_decisions (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id         INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    conversation_id     INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id          INTEGER,
    request_id          VARCHAR(100),
    experience_id       UUID,   -- FK to language_experiences.experience_id (soft reference;
                                -- language_experiences is written after routing, so a hard FK
                                -- would require deferred constraints that add complexity for
                                -- little safety gain here)

    -- What the router decided
    routed_to           VARCHAR(10) NOT NULL,   -- 'brain' | 'llm' | 'shadow'
    predicted_intent    VARCHAR(100),           -- brain's prediction at decision time
    similarity          FLOAT,                  -- raw cosine similarity
    calibrated_threshold FLOAT,                 -- threshold used at decision time (snapshot)
    agreement_rate      FLOAT,                  -- agreement_rate at decision time (snapshot)
    brain_version       VARCHAR(50),

    -- Outcome (filled in by core_agent.py after the Kernel returns,
    -- via UPDATE -- same pattern as agent_run_traces.status). NULL
    -- until the Kernel run completes.
    final_intent        VARCHAR(100),           -- what the Kernel actually resolved
    was_correct         BOOLEAN,                -- predicted_intent == final_intent (for 'brain' routes only)

    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_routing_decisions_tenant
    ON routing_decisions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_routing_decisions_tenant_created
    ON routing_decisions(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_routing_decisions_routed_to
    ON routing_decisions(tenant_id, routed_to);


-- ─── 3. Calibration run log ──────────────────────────────────────────
-- One row per CalibrationService.run_for_tenant() call. Lets the
-- operator query how calibration is progressing over time without
-- reading intent_clusters directly.

CREATE TABLE IF NOT EXISTS calibration_runs (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    run_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    intents_updated     INTEGER NOT NULL DEFAULT 0,  -- how many intent_clusters rows were updated
    total_samples       INTEGER NOT NULL DEFAULT 0,  -- total brain_used=TRUE rows examined
    min_agreement_rate  FLOAT NOT NULL,              -- threshold used for this run
    min_sample_count    INTEGER NOT NULL,            -- minimum samples required before calibrating
    notes               TEXT                         -- optional human/system annotation
);

CREATE INDEX IF NOT EXISTS idx_calibration_runs_tenant
    ON calibration_runs(tenant_id, run_at DESC);
