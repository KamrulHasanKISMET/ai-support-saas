-- =====================================================================
-- AGENT RUN TRACE
-- Structured trace of one Core Agent execution (one customer message
-- in, one reply out). NOT a full analytics platform -- this is a
-- write path + schema only. No read/query API is added in this
-- migration; query this table directly with SQL for now.
--
-- Written by python-api/app/trace/trace_service.py, called from
-- CoreAgent.run() (app/agent/core_agent.py) after the Kernel returns.
-- Isolated try/except around the write -- a trace failure NEVER blocks
-- or alters the customer-facing reply (same pattern as Memory
-- extraction in app/kernel/kernel.py).
-- =====================================================================

CREATE TABLE agent_run_traces (
    id                      SERIAL PRIMARY KEY,
    agent_run_id            UUID NOT NULL,           -- one per CoreAgent.run() call
    request_id              VARCHAR(100),            -- cross-service trace id, see middleware/requestId.ts

    -- Identifiers (architecture doc: these must always travel together)
    tenant_id               INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id              INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    conversation_id          INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id                INTEGER,                -- node-api's message id; nullable (not FK'd --
                                                        -- messages live in node-api's request, not a
                                                        -- guaranteed-existing row from python-api's view)

    -- Language Engine output
    detected_language        VARCHAR(20),
    reply_language            VARCHAR(20),
    normalized_input          TEXT,

    -- Intent Engine output
    intent                    VARCHAR(100),
    confidence                 DECIMAL(4,3),

    -- State Engine output (conversation_states snapshot at this turn)
    state                      JSONB NOT NULL DEFAULT '{}',

    -- Context Engine / RAG usage
    retrieval_used             BOOLEAN NOT NULL DEFAULT FALSE,
    retrieval_chunk_count       INTEGER NOT NULL DEFAULT 0,

    -- Kernel decision + tools
    decision                   VARCHAR(50),            -- 'clarify' | 'answered' | 'fallback'
    tools_called                JSONB NOT NULL DEFAULT '[]',

    -- Output
    response                   TEXT,

    -- Model metadata
    model                       VARCHAR(100),

    -- Performance / cost
    latency_ms                  INTEGER,
    cost_usd                    DECIMAL(10,6),          -- nullable placeholder; no per-model pricing
                                                         -- table exists yet, so this is not populated
                                                         -- yet -- see docs/AGENT.md

    -- Errors
    error                        TEXT,                  -- set only when the Kernel's outer fallback fired

    created_at                   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_agent_run_traces_tenant ON agent_run_traces(tenant_id);
CREATE INDEX idx_agent_run_traces_tenant_conversation ON agent_run_traces(tenant_id, conversation_id);
CREATE INDEX idx_agent_run_traces_agent_run_id ON agent_run_traces(agent_run_id);
