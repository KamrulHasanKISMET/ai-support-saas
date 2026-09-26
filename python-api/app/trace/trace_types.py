"""
AGENT RUN TRACE — types.

One AgentRunTrace = one CoreAgent.run() execution. Mirrors the
`agent_run_traces` table (db/init/005_agent_foundation.sql) field for
field. This is a write-path data carrier, not an analytics model --
no aggregation/query helpers live here (see trace_service.py's
docstring for why).
"""

from dataclasses import dataclass, field
from uuid import UUID


@dataclass
class AgentRunTrace:
    agent_run_id: UUID
    tenant_id: int
    customer_id: int
    conversation_id: int
    message_id: int | None = None
    request_id: str | None = None

    detected_language: str | None = None
    reply_language: str | None = None
    normalized_input: str | None = None

    intent: str | None = None
    confidence: float | None = None

    state: dict = field(default_factory=dict)

    retrieval_used: bool = False
    retrieval_chunk_count: int = 0

    decision: str | None = None
    tools_called: list[str] = field(default_factory=list)

    response: str | None = None

    model: str | None = None

    latency_ms: int | None = None
    cost_usd: float | None = None  # not populated yet -- see trace_service.py

    error: str | None = None

    # Per-step latency breakdown (Phase 1 observability,
    # db/init/007_observability.sql / docs/OBSERVABILITY.md). Mirrors
    # KernelRunResponse's new fields (schemas/kernel.py) exactly.
    language_latency_ms: int | None = None
    intent_latency_ms: int | None = None
    context_latency_ms: int | None = None
    memory_latency_ms: int | None = None
    rag_latency_ms: int | None = None
    llm_latency_ms: int | None = None
    error_category: str | None = None  # ErrorCategory value; see app/core/error_types.py
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Commercial V1 trace lifecycle (db/init/008_trace_lifecycle.sql).
    # trace_id completes the correlation chain: request_id -> trace_id
    # -> agent_run_id -> tenant_id -> customer_id -> conversation_id ->
    # message_id -> channel. See that migration's header comment for
    # why trace_id is kept distinct from request_id even though they
    # carry the same value today.
    trace_id: str | None = None
    channel: str | None = None
    status: str = "completed"  # 'completed' | 'partial' | 'error'
    failed_step: str | None = None
    steps: list[dict] = field(default_factory=list)
