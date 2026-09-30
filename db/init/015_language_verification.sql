-- =====================================================================
-- LANGUAGE VERIFICATION LEVELS (Phase 4 — corrected scope)
-- docs/GENERAL_LANGUAGE_BRAIN.md §2 (evidence hierarchy), §9 item 2.
--
-- Problem this fixes: `language_experiences.learning_eligible` used to
-- be the ONLY gate ClusterBuilderService consulted before treating a
-- row as trusted training material. That is "LLM says X -> automatically
-- teach the Brain X" -- exactly the anti-pattern Correction #2
-- prohibits. `final_intent` is a CANDIDATE interpretation from the LLM
-- Teacher, not verified ground truth, until something downstream
-- (brain/LLM agreement, a completed order, a human's confirmation)
-- actually checks it.
--
-- This migration adds the real state machine (§2.2) alongside the
-- existing `verification_result` column, which is left in place
-- unused rather than dropped -- nothing ever read it, so there is
-- nothing to migrate off of it, and dropping a column for no
-- functional reason is unnecessary churn.
--
-- Additive only, same defensive-parsing discipline used everywhere
-- else in this schema: no existing column changes meaning, no rows
-- need backfilling (every existing row is legitimately 'unverified' --
-- that was already the truth, just not distinguished from
-- 'self_consistent'/'outcome_positive'/etc. before now).
-- =====================================================================

ALTER TABLE language_experiences
    ADD COLUMN IF NOT EXISTS verification_level VARCHAR(20) NOT NULL DEFAULT 'unverified',
        -- 'unverified'        -- recorded, nothing has checked it (default)
        -- 'self_consistent'   -- own Brain and LLM agreed (provisional
        --                        evidence -- how the system bootstraps
        --                        before enough outcome/human signal
        --                        exists; gated further by
        --                        source_reliability below)
        -- 'outcome_positive'  -- a downstream business signal confirmed it
        -- 'outcome_negative'  -- a downstream signal contradicted it
        -- 'human_confirmed'   -- a human explicitly labeled it correct
        -- 'human_corrected'   -- a human explicitly relabeled it (the
        --                        old label is superseded, see below --
        --                        never deleted)
    ADD COLUMN IF NOT EXISTS source_reliability FLOAT,
        -- Only meaningful when verification_level='self_consistent':
        -- the calibrated agreement_rate of the cluster the brain
        -- agreed with, snapshotted at write time (app/agent/core_agent.py).
        -- NULL for every other verification_level. Compared against
        -- each tenant's min_reliability (tenant_calibration_config,
        -- below) by ClusterBuilderService's eligibility gate (§2.3):
        --
        --   verification_level IN ('outcome_positive', 'human_confirmed')
        --     OR (verification_level = 'self_consistent'
        --         AND source_reliability >= tenant_config.min_reliability)
    ADD COLUMN IF NOT EXISTS superseded_by UUID;
        -- Soft pointer to another language_experiences.experience_id:
        -- when two verified signals for the same underlying
        -- message/cluster disagree (e.g. human_confirmed intent A
        -- today, human_corrected to intent B next week), the newer
        -- verified label wins for future learning but the OLD row is
        -- retained with this pointer set -- never deleted. Same
        -- append-only philosophy cluster_promotion_log already uses
        -- for promotion events (§2.4). NULL = not superseded. Nothing
        -- writes this column yet (no label-revision UI exists) -- it
        -- is added now so that future UI has a column to write to
        -- without another migration.

CREATE INDEX IF NOT EXISTS idx_language_experiences_tenant_verification
    ON language_experiences(tenant_id, verification_level);

-- ─── Per-tenant reliability threshold ─────────────────────────────────
-- Same additive, per-tenant-override pattern as every other column on
-- this table (min_agreement_rate, min_sample_count, ...) -- no new
-- mechanism class, just one more tunable per §2.3's eligibility gate.

ALTER TABLE tenant_calibration_config
    ADD COLUMN IF NOT EXISTS min_reliability FLOAT NOT NULL DEFAULT 0.90;
        -- Minimum source_reliability for a 'self_consistent' row to
        -- count as an eligible learning signal on its own (without an
        -- outcome/human verification). A high-stakes tenant can raise
        -- this the same way it can raise min_agreement_rate today.
