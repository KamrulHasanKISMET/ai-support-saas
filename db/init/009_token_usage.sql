-- =====================================================================
-- AGENT RUN TRACE -- Token usage persistence
--
-- Extends the SAME agent_run_traces table once more (005, 007, 008
-- before this) -- no new table.
--
-- input_tokens / output_tokens represent the AGGREGATE (sum) of every
-- Anthropic completion call made during one agent run, not just the
-- final reply-generation call. One kernel.run() can make up to four
-- such calls today: language understanding, intent classification,
-- reply generation, and memory extraction (all via
-- app/ai/ai_service.py's complete()/complete_json()) -- see
-- docs/AGENT.md for the verified call inventory. This mirrors how the
-- existing top-level `latency_ms` is already the whole run's total,
-- not just one step's.
--
-- NULL means "no usage information was available for this run" (e.g.
-- every contributing call failed before the provider returned usage).
-- 0 is a distinct, valid value meaning "usage was reported and was
-- zero". These are never conflated -- see app/kernel/kernel.py's
-- accumulator and tests/test_agent_foundation.py's regression tests.
-- =====================================================================

ALTER TABLE agent_run_traces
    ADD COLUMN input_tokens  INTEGER,
    ADD COLUMN output_tokens INTEGER;
