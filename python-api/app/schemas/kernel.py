from pydantic import BaseModel


class KernelRunRequest(BaseModel):
    tenantId: int
    customerId: int
    conversationId: int
    messageId: int
    message: str


class KernelRunResponse(BaseModel):
    reply: str
    intent: str | None = None
    confidence: float | None = None
    state: dict = {}
    toolsCalled: list[str] = []

    # Set by the Kernel from the Language Engine's result (section 14).
    # Optional with defaults, so this is additive — anything already
    # constructing a KernelRunResponse without these still works.
    detectedLanguage: str | None = None
    replyLanguage: str | None = None

    # Added for Agent Run Trace (docs/AGENT.md) — all optional with
    # defaults, purely additive, same non-breaking pattern as above.
    # CoreAgent reads these to build an AgentRunTrace; nothing about
    # the Kernel's control flow, prompts, or error boundaries changed
    # to add them, only what's already-computed data gets returned.
    normalizedMessage: str | None = None
    decision: str | None = None  # 'clarify' | 'answered' | 'fallback'
    retrievalUsed: bool = False
    retrievalChunkCount: int = 0
    errorOccurred: bool = False  # True only on the Kernel's outer-catch fallback path

    # Per-step latency breakdown (Phase 1 observability, docs/OBSERVABILITY.md).
    # Mirrors db/init/007_observability.sql's new agent_run_traces columns
    # exactly. Measured around the SAME calls the Kernel already makes —
    # no new calls, no control-flow change, just timing wrapped around
    # existing steps. Any step skipped on a given run (e.g. context/RAG
    # on the low-confidence "clarify" path) simply stays None.
    languageLatencyMs: int | None = None
    intentLatencyMs: int | None = None
    contextLatencyMs: int | None = None
    memoryLatencyMs: int | None = None  # memory RETRIEVAL (read), not extraction/write
    ragLatencyMs: int | None = None
    llmLatencyMs: int | None = None  # the final reply-generating LLM call only
    inputTokens: int | None = None
    outputTokens: int | None = None
    kernelLatencyMs: int | None = None  # total time inside Kernel.run() -- distinct from
    # CoreAgent's own total agent-run latency (that also includes config
    # load + trace write, measured separately in core_agent.py)
    errorCategory: str | None = None  # ErrorCategory value; set only on the fallback path

    # Commercial V1 trace lifecycle (docs/OBSERVABILITY.md,
    # db/init/008_trace_lifecycle.sql). All additive/defaulted --
    # existing construction of KernelRunResponse elsewhere (there is
    # none outside kernel.py today, but this keeps the same
    # non-breaking pattern as every prior addition to this class).
    status: str = "completed"  # 'completed' | 'partial' | 'error'
    failedStep: str | None = None  # which lifecycle stage was in progress on failure
    steps: list[dict] = []  # ordered [{"step", "status", "durationMs"?, "error"?, "metadata"?}]
