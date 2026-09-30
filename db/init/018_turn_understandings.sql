-- =====================================================================
-- TURN UNDERSTANDINGS -- Phase 5, capability A (PENDING_WORK.md C1)
-- app/language/glb.py::TurnUnderstanding, persisted.
--
-- One row per customer turn: WHAT the system understood (language tag,
-- script, transliteration, code-mixing, intent + where it came from,
-- the four control-plane eligibility flags, recurring-phenomena flags).
--
-- PRIVACY / SCOPE (docs/GENERAL_LANGUAGE_BRAIN.md §5.3):
--   * NO message text and NO normalized text here -- that lives, under
--     its own rules, in language_experiences. This table is metadata
--     only.
--   * NO field about the person (nationality, ethnicity, location,
--     identity). Language/script are data-quality signals only.
--
-- RETENTION: 90 days (decision recorded 2026-09-28). expires_at is set
-- at insert; app/language/understanding_store.py::purge_expired() and
-- `python -m app.language.understanding_retention` delete expired rows.
-- The 90 is also TURN_UNDERSTANDING_RETENTION_DAYS in that module; a
-- test fails if the two disagree. NOTHING schedules the purge yet --
-- see docs/PENDING_WORK.md C1a. Until it is scheduled the table grows.
--
-- Rows are also removed with their tenant / customer / conversation
-- (ON DELETE CASCADE), like every other per-turn table.
-- =====================================================================

CREATE TABLE IF NOT EXISTS turn_understandings (
    id                          BIGSERIAL PRIMARY KEY,
    tenant_id                   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id                 INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    conversation_id             INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id                  INTEGER,
    request_id                  VARCHAR(100),
    experience_id               UUID,            -- soft FK to language_experiences.experience_id

    -- surface (language layer). 'und' = we have no signal (not a guess).
    language                    VARCHAR(30) NOT NULL DEFAULT 'und',
    script                      VARCHAR(30) NOT NULL DEFAULT 'und',
    is_transliterated           BOOLEAN NOT NULL DEFAULT FALSE,
    code_mixing                 VARCHAR(30) NOT NULL DEFAULT 'none',
    reply_language              VARCHAR(30),

    -- meaning (capability B)
    intent                      VARCHAR(50),
    intent_confidence           FLOAT,
    intent_source               VARCHAR(20),     -- 'brain' | 'llm' | NULL

    -- control plane: four independent flags (§3)
    language_learning_eligible  BOOLEAN NOT NULL DEFAULT TRUE,
    automation_eligible         BOOLEAN NOT NULL DEFAULT TRUE,
    promotion_eligible          BOOLEAN NOT NULL DEFAULT TRUE,
    training_eligible           BOOLEAN NOT NULL DEFAULT TRUE,

    -- recurring phenomena: code_mixed | transliterated | ambiguous |
    -- novel | low_confidence  (open set; new mechanisms add values)
    phenomena                   TEXT[] NOT NULL DEFAULT '{}',

    created_at                  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at                  TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP + INTERVAL '90 days')
);

-- Tenant dashboards / evaluation slices, newest first.
CREATE INDEX IF NOT EXISTS idx_turn_understandings_tenant_created
    ON turn_understandings (tenant_id, created_at DESC);

-- The retention purge scans by expiry.
CREATE INDEX IF NOT EXISTS idx_turn_understandings_expires
    ON turn_understandings (expires_at);

-- "Show me every code-mixed / novel turn" without a table scan.
CREATE INDEX IF NOT EXISTS idx_turn_understandings_phenomena
    ON turn_understandings USING GIN (phenomena);
