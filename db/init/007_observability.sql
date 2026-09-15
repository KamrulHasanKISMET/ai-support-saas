-- =====================================================================
-- OBSERVABILITY FOUNDATION (Phase 1)
--
-- Adds per-step latency breakdown to the existing agent_run_traces
-- table (db/init/005_agent_foundation.sql) instead of creating a
-- second tracing system. All new columns are nullable with no
-- default-value backfill needed -- existing rows simply have NULL
-- here, which is accurate (that data was never measured for them).
--
-- Reused, not duplicated: this is the SAME table CoreAgent already
-- writes to via app/trace/trace_service.py. See docs/OBSERVABILITY.md.
-- =====================================================================

ALTER TABLE agent_run_traces
    ADD COLUMN language_latency_ms  INTEGER,
    ADD COLUMN intent_latency_ms    INTEGER,
    ADD COLUMN context_latency_ms   INTEGER,
    ADD COLUMN memory_latency_ms    INTEGER,
    ADD COLUMN rag_latency_ms       INTEGER,
    ADD COLUMN llm_latency_ms       INTEGER,
    ADD COLUMN error_category       VARCHAR(50); -- see app/core/error_types.py for the taxonomy
