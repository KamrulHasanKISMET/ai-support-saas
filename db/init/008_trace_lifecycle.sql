-- =====================================================================
-- AGENT RUN TRACE -- Commercial V1 lifecycle hardening
--
-- Extends the SAME agent_run_traces table (005_agent_foundation.sql,
-- 007_observability.sql) once more -- still one table, no second
-- tracing system. Adds:
--
--   trace_id     -- completes the correlation chain the task asked for:
--                   request_id -> trace_id -> agent_run_id -> tenant_id
--                   -> customer_id -> conversation_id -> message_id ->
--                   channel. In practice trace_id == request_id today
--                   (one request currently maps to exactly one agent
--                   run), but it's stored as its own column/concept so
--                   a future single request that spans multiple
--                   correlated agent runs (e.g. a tool call that
--                   triggers a sub-run) has a stable id to group them
--                   under, without overloading request_id's meaning.
--   channel      -- was available on AgentRequest.channel the whole
--                   time but never reached the trace -- same class of
--                   gap as the per-step latency columns in
--                   007_observability.sql were before that migration.
--   status       -- overall run outcome: 'completed' | 'partial' |
--                   'error'. 'partial' is new: previously a degraded
--                   RAG/Memory retrieval (see docs/RAG.md, docs/MEMORY.md
--                   -- these already fail gracefully and were already
--                   invisible at the trace level before this migration)
--                   looked identical to a fully healthy run in the trace.
--   failed_step  -- which lifecycle stage was in progress when status
--                   is 'partial'/'error'. NULL when status='completed'.
--   steps        -- ordered JSONB array, one entry per lifecycle stage
--                   that actually ran this turn: tenant_context,
--                   language, intent, context, memory, rag, tool (only
--                   when applicable), llm, memory_update. Each entry:
--                   {"step": "...", "status": "ok"|"error"|"skipped",
--                   "durationMs": <int, when applicable>, "error": "...",
--                   "metadata": {...}}. See docs/OBSERVABILITY.md.
--
-- Everything already in agent_run_traces (per-step *_latency_ms
-- columns, detected_language, intent, etc.) is UNCHANGED and still
-- populated exactly as before -- `steps` is a complementary, more
-- structured view of largely the same measurements, not a replacement.
-- =====================================================================

ALTER TABLE agent_run_traces
    ADD COLUMN trace_id    VARCHAR(100),
    ADD COLUMN channel     VARCHAR(50),
    ADD COLUMN status      VARCHAR(20) NOT NULL DEFAULT 'completed',
    ADD COLUMN failed_step VARCHAR(50),
    ADD COLUMN steps       JSONB NOT NULL DEFAULT '[]';

CREATE INDEX idx_agent_run_traces_trace_id ON agent_run_traces(trace_id);
CREATE INDEX idx_agent_run_traces_tenant_status ON agent_run_traces(tenant_id, status);
