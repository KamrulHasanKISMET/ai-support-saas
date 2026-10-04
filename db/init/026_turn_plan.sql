-- =====================================================================
-- TURN PLAN COLUMNS -- Phase 5, P5-4a (docs/PHASE_5_8_PLAN.md)
-- Persist the ADVISORY glb_orchestrator.TurnPlan next to the
-- turn_understandings row it was derived from, so a later session can
-- compare "what the plan said" with "what really happened" (P5-4).
--
-- Additive + idempotent (ADD COLUMN IF NOT EXISTS); old rows stay NULL
-- forever (no backfill: a plan cannot be reconstructed after the fact).
-- Same row => same expires_at => the existing 90-day purge covers it,
-- so no new retention job or table.
--
-- PRIVACY (GENERAL_LANGUAGE_BRAIN.md §5.3): metadata only. No message
-- text, no person-inference field. `plan_reasons` holds short machine
-- codes such as 'teacher_required:novel', never user text.
--
-- The plan is ADVISORY. `enacted` is always false and is deliberately
-- NOT stored (a constant carries no information); nothing reads these
-- columns to make a decision.
-- =====================================================================

ALTER TABLE turn_understandings
    ADD COLUMN IF NOT EXISTS plan_understanding_source VARCHAR(30),   -- 'own_model_ok' | 'llm_teacher_required'
    ADD COLUMN IF NOT EXISTS plan_automation           VARCHAR(30),   -- 'allowed' | 'human_review_required'
    ADD COLUMN IF NOT EXISTS plan_clarify              BOOLEAN,
    ADD COLUMN IF NOT EXISTS plan_triage               BOOLEAN,
    ADD COLUMN IF NOT EXISTS plan_record_as_learning   BOOLEAN,
    ADD COLUMN IF NOT EXISTS plan_reasons              TEXT[];
