-- =====================================================================
-- PHASE 3 — AUTOMATED LEARNING / PROMOTION PIPELINE
-- docs/LANGUAGE_INTELLIGENCE.md Phase 3
--
-- What this adds:
--   1. Version + lifecycle columns on intent_clusters: every row gets
--      a version string, promoted_at, retired_at, and parent_cluster_id
--      so the full lineage of a cluster is traceable.
--   2. cluster_promotion_log: one row per promotion or rollback event,
--      keyed by (tenant_id, intent, version). This is the audit trail
--      for the shadow-eval → canary → promote/rollback lifecycle.
--   3. canary_splits: per-tenant canary traffic allocation (what % of
--      brain-eligible traffic should go to the candidate cluster vs the
--      current promoted one). Managed by PromotionService; read by
--      ClusterBuilderService at routing time.
--
-- Tenant isolation: every table here requires tenant_id (NOT NULL, FK).
-- Cluster lifecycle states (cluster_promotion_log.event_type):
--   'shadow_eval'  -- new cluster is being evaluated in shadow
--   'canary_start' -- canary traffic split activated
--   'canary_pass'  -- canary met accuracy threshold, promoted to live
--   'canary_fail'  -- canary fell below threshold, rolled back
--   'rollback'     -- manual rollback of a promoted cluster
--   'retired'      -- cluster superseded by a newer promotion
-- =====================================================================


-- ─── 1. Lifecycle columns on intent_clusters ─────────────────────────

ALTER TABLE intent_clusters
    ADD COLUMN IF NOT EXISTS version           VARCHAR(60),
        -- Opaque version string set at build time by ClusterBuilderService.
        -- Format: '<source>-v<n>-<yyyymmdd>' e.g. 'tenant_learned-v1-20260927'.
        -- NULL on legacy default_seed rows (they predate versioning).
    ADD COLUMN IF NOT EXISTS is_promoted       BOOLEAN NOT NULL DEFAULT FALSE,
        -- TRUE: this cluster is the active live cluster for (tenant_id, intent).
        -- Only ONE row per (tenant_id, intent) should have is_promoted=TRUE
        -- at any time. Enforced by PromotionService (application-level);
        -- a partial-unique index below catches violations in tests.
    ADD COLUMN IF NOT EXISTS is_candidate      BOOLEAN NOT NULL DEFAULT FALSE,
        -- TRUE: this cluster is the current canary candidate for
        -- (tenant_id, intent). At most one per (tenant_id, intent).
    ADD COLUMN IF NOT EXISTS promoted_at       TIMESTAMP,
        -- When this cluster became the live cluster (is_promoted flipped TRUE).
    ADD COLUMN IF NOT EXISTS retired_at        TIMESTAMP,
        -- When this cluster was superseded (is_promoted flipped FALSE by a newer
        -- promotion). NULL if still active or never promoted.
    ADD COLUMN IF NOT EXISTS parent_cluster_id INTEGER REFERENCES intent_clusters(id);
        -- The cluster this one was built from (lineage tracing). NULL for
        -- default_seed rows (they have no parent).

-- Partial unique index: at most one promoted cluster per (tenant, intent).
-- Allows multiple non-promoted rows (candidates, retired) with no conflict.
CREATE UNIQUE INDEX IF NOT EXISTS uq_intent_clusters_promoted
    ON intent_clusters(tenant_id, intent)
    WHERE is_promoted = TRUE;

-- Partial unique index: at most one candidate cluster per (tenant, intent).
CREATE UNIQUE INDEX IF NOT EXISTS uq_intent_clusters_candidate
    ON intent_clusters(tenant_id, intent)
    WHERE is_candidate = TRUE;


-- ─── 2. Cluster promotion log ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS cluster_promotion_log (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    intent          VARCHAR(100) NOT NULL,
    cluster_id      INTEGER REFERENCES intent_clusters(id),
    version         VARCHAR(60),
    event_type      VARCHAR(20) NOT NULL,
        -- 'shadow_eval' | 'canary_start' | 'canary_pass' |
        -- 'canary_fail' | 'rollback' | 'retired'
    event_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Metrics snapshot at event time (for post-hoc analysis)
    sample_count    INTEGER,           -- brain_used=TRUE rows examined
    agreement_rate  FLOAT,            -- at canary_pass / canary_fail time
    was_correct_rate FLOAT,           -- routing_decisions.was_correct rate at decision time
    canary_pct      FLOAT,            -- traffic % routed to candidate at this event

    notes           TEXT               -- optional human/system annotation
);

CREATE INDEX IF NOT EXISTS idx_cluster_promotion_log_tenant
    ON cluster_promotion_log(tenant_id, intent, event_at DESC);


-- ─── 3. Canary splits ────────────────────────────────────────────────
-- Per-(tenant, intent) canary traffic allocation. When a candidate
-- cluster exists, RoutingService reads canary_pct to decide whether
-- to route a brain-eligible turn to the promoted cluster or the
-- candidate. 0 = no canary active (all to promoted). 100 = full
-- promotion (should not exist long -- PromotionService promotes and
-- clears this row).

CREATE TABLE IF NOT EXISTS canary_splits (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    intent          VARCHAR(100) NOT NULL,
    candidate_cluster_id INTEGER NOT NULL REFERENCES intent_clusters(id),
    canary_pct      FLOAT NOT NULL DEFAULT 5.0,
        -- Percentage (0-100) of brain-eligible traffic for this intent
        -- routed to the candidate cluster. Starts at 5% per the design
        -- doc's conservative canary strategy.
    started_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(tenant_id, intent)  -- one active canary per (tenant, intent)
);

CREATE INDEX IF NOT EXISTS idx_canary_splits_tenant
    ON canary_splits(tenant_id);
