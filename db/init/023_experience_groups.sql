-- =====================================================================
-- EXPERIENCE GROUPS -- P6D-4 (schema only; NOT wired to the write path
-- yet -- see docs/EXPERIENCE_DEDUPLICATION_AND_RETENTION.md and
-- docs/TRAINING_GRADE_DATA_TASK.md "Data quality & diversity").
--
-- One row per DISTINCT (tenant, intent, language, shape) canonical
-- pattern seen in `language_experiences`
-- (app/language/experience_shape.py computes `shape`). This table is
-- created now so the schema exists and can be reviewed, but nothing
-- writes to it yet: `experience_backfill_report.py` (P6D-8, read-only)
-- must run against real traffic first to pick a sane reservoir cap M
-- (owner decision P6D-1) before the write path (P6D-6, still TODO)
-- starts populating it. Until P6D-6 ships, this table stays empty --
-- that is expected, not a bug.
--
-- sample_experience_ids holds AT MOST M experience_id values (a
-- reservoir sample, P6D-6) -- never the raw text itself; raw text stays
-- only in language_experiences, one row per kept sample, so retention
-- (P6D-2) can purge raw text there while this table's counters and
-- shape survive untouched.
--
-- Existing databases: apply with scripts/migrate.sh 023 (idempotent).
-- Safe to apply even with nothing writing to it yet.
-- =====================================================================

CREATE TABLE IF NOT EXISTS experience_groups (
    id                     BIGSERIAL PRIMARY KEY,
    tenant_id              INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    intent                 VARCHAR(50),
    language               VARCHAR(30) NOT NULL DEFAULT 'und',
    shape_hash             CHAR(64) NOT NULL,          -- app/language/experience_shape.py
    observed_count         BIGINT NOT NULL DEFAULT 0,
    sample_experience_ids  UUID[] NOT NULL DEFAULT '{}',  -- reservoir, size <= reservoir_cap
    reservoir_cap          INTEGER NOT NULL DEFAULT 50,   -- placeholder; owner decision P6D-1
    best_verification_level VARCHAR(30),                  -- highest level seen; never downgrades
    first_seen_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT experience_groups_uniq UNIQUE (tenant_id, intent, language, shape_hash)
);

CREATE INDEX IF NOT EXISTS idx_experience_groups_tenant
    ON experience_groups (tenant_id, intent, language);
CREATE INDEX IF NOT EXISTS idx_experience_groups_observed
    ON experience_groups (tenant_id, observed_count DESC);
