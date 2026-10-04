-- 019_routing_served_branch.sql
--
-- PENDING_WORK.md C8: record WHICH cluster/branch served a routed turn.
--
-- Why: the auto canary ramp (canary_ramp_service.py) measures a stage's
-- accuracy from routing_decisions rows, but until now those rows did not
-- say whether the candidate or the promoted cluster served them, so at
-- partial percentages the "candidate" accuracy was mostly the promoted
-- cluster's. With these columns the ramp counts only candidate-served rows.
--
-- served_branch:
--   'candidate' -- canary branch taken; the candidate cluster's threshold decided
--   'promoted'  -- an is_promoted cluster's threshold decided
--   'other'     -- a calibrated, non-promoted cluster decided (e.g. default seed)
--   NULL        -- nothing was served from a cluster (no prediction /
--                  uncalibrated), or the row predates this migration
--
-- Rows written before this migration stay NULL forever (we cannot know).
-- The ramp therefore ignores them: an in-flight canary restarts its stage
-- evidence from zero candidate-served samples after deploy, which is the
-- correct, conservative behaviour. Never edit older migrations.
-- Existing databases: db/init only auto-runs on a fresh volume, so apply
-- this file by hand (it is idempotent).

ALTER TABLE routing_decisions
    ADD COLUMN IF NOT EXISTS served_cluster_id INTEGER,
    ADD COLUMN IF NOT EXISTS served_branch     VARCHAR(10);

-- Soft reference (no FK): clusters can be retired/deleted while history stays.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'routing_decisions_served_branch_chk'
    ) THEN
        ALTER TABLE routing_decisions
            ADD CONSTRAINT routing_decisions_served_branch_chk
            CHECK (served_branch IS NULL OR served_branch IN ('candidate', 'promoted', 'other'));
    END IF;
END $$;

-- Serves the ramp's stage query (tenant + intent + candidate + time window).
CREATE INDEX IF NOT EXISTS idx_routing_decisions_served
    ON routing_decisions (tenant_id, predicted_intent, served_cluster_id, created_at)
    WHERE routed_to = 'brain';
