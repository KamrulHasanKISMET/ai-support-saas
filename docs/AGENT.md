# docs/AGENT.md

**Files:** `python-api/app/agent/core_agent.py`, `agent_config.py`,
`tenant_brain.py`. **Trace:** `python-api/app/trace/` (`trace_types.py`,
`trace_service.py`). **Schema:** `python-api/app/schemas/agent.py`
(`AgentRequest`/`AgentResponse`).

## What Core Agent is

The single class `CoreAgent`, one public method `run(db, request)`.
Called from `app/api/routes/ai.py` — this is the actual body of
`POST /ai/kernel/run`.

```python
async def run(self, db, request: AgentRequest) -> AgentResponse:
    agent_run_id = uuid.uuid4()
    started_at = time.perf_counter()

    try:
        tenant_config = await load_agent_config(db, request.tenantId)
    except Exception:
        logger.error(...)
        tenant_config = self._fallback_config
    logger.info(..., agent_run_id, tenant_config.vertical, tenant_config.tools_enabled)

    kernel_result = await kernel.run(
        db=db, tenant_id=request.tenantId, customer_id=request.customerId,
        conversation_id=request.conversationId, message=request.message,
        request_id=request.requestId,
    )
    latency_ms = int((time.perf_counter() - started_at) * 1000)

    await record_trace(db, AgentRunTrace(
        agent_run_id=agent_run_id, tenant_id=request.tenantId, ...,
        latency_ms=latency_ms, model=settings.llm_model,
        error="kernel_fallback" if kernel_result.errorOccurred else None,
    ))

    return AgentResponse(
        reply=kernel_result.reply, intent=kernel_result.intent,
        confidence=kernel_result.confidence, state=kernel_result.state,
        toolsCalled=kernel_result.toolsCalled,
        metadata={**request.metadata, "language": {
            "detected": kernel_result.detectedLanguage,
            "reply": kernel_result.replyLanguage,
        }},
    )
```

That's the entire class body. It:
1. Generates an `agent_run_id` (uuid4) — one per Core Agent execution,
   the anchor id for this run's Agent Run Trace (see below).
2. Logs the incoming request (tenant/customer/conversation/channel).
3. Loads this tenant's `AgentConfig` — the first of two DB operations
   this class makes, kept in its own function rather than inlined.
4. Calls `kernel.run(...)` — no other engine, no embedding call, no
   LLM call happens directly in this class.
5. Records an `AgentRunTrace` (`app/trace/`) — the second DB
   operation, isolated so a trace-write failure can never affect the
   reply already built. See "Agent Run Trace" below.
6. Reshapes the Kernel's result into `AgentResponse`, folding
   `detectedLanguage`/`replyLanguage` into `metadata.language` alongside
   whatever metadata the caller sent. **Unchanged from before** — the
   external Node-facing contract did not change in this version.

## Agent Run Trace — wired and tested

**Files:** `app/trace/trace_types.py` (`AgentRunTrace` dataclass, mirrors
the `agent_run_traces` table column-for-column), `app/trace/trace_service.py`
(`record_trace(db, trace)` — a single INSERT + commit).

`CoreAgent.run()` calls `record_trace()` once, right after `kernel.run()`
returns and right before building the `AgentResponse`. Isolated
try/except inside `record_trace()` itself (not in `CoreAgent`) means a
trace-write failure is logged and swallowed — it can never delay or
alter the reply that's about to go back to the customer. Exactly the
same isolation philosophy already used for Memory extraction inside
`kernel.run()`.

To make the trace meaningful, `KernelRunResponse` (`schemas/kernel.py`)
gained four more optional fields this version, all additive:
`normalizedMessage`, `decision` (`'clarify' | 'answered' | 'fallback'`),
`retrievalUsed`, `retrievalChunkCount`, plus `errorOccurred` (true only
on the Kernel's outer-catch fallback path). `kernel.py`'s control flow,
prompts, and error boundaries did **not** change to add these — the
same three response-construction sites (clarify branch, normal branch,
outer fallback) just populate a few more fields on the object they
were already building. See `docs/KERNEL.md`.

**Write-only, deliberately.** No read/query endpoint exists for
`agent_run_traces` in this version — "do not build a complete
analytics platform yet" per the task constraint. Query it directly
with SQL: `SELECT * FROM agent_run_traces WHERE tenant_id = ... ORDER
BY created_at DESC`.

**Not yet captured:** `cost_usd` stays `NULL` — no per-model token
pricing table exists yet to compute it from the token-usage numbers
`ai_service.complete()` already logs.

### Commercial V1 lifecycle hardening (this version)

**Migration:** `db/init/008_trace_lifecycle.sql` — five more columns
on the same `agent_run_traces` table, no second table, no second
tracing system.

**Correlation chain, now complete end to end:**
```
request_id (node-api) → trace_id → agent_run_id → tenant_id →
customer_id → conversation_id → message_id → channel
```
`trace_id` is a new concept, deliberately kept distinct from
`request_id` even though `CoreAgent.run()` sets `trace_id =
request.requestId or str(uuid.uuid4())` — i.e. they hold the *same
value* today. The distinction exists for when a future capability
(e.g. a Tool call that triggers its own sub-agent-run) needs one
`trace_id` to span several `agent_run_id`s; that consumer doesn't
exist yet, so today this is a 1:1 mapping, not speculative machinery.
`channel` was sitting on `AgentRequest.channel` the whole time
(`schemas/agent.py`) but never reached the trace before this version —
same class of gap as the per-step latency columns were before
`007_observability.sql`.

**Lifecycle visibility.** `KernelRunResponse` gained `status`
(`'completed' | 'partial' | 'error'`), `failedStep`, and `steps` — an
ordered list of `{"step", "status", "durationMs"?, "error"?,
"metadata"?}` entries, one per lifecycle stage that actually ran this
turn: `language`, `intent`, `context`, `memory`, `rag`, `tool` (only
present when an order/status intent was recognized — see
`docs/KERNEL.md`), `llm`, `memory_update`. This is a structured,
queryable complement to the existing flat `*_latency_ms` columns, not
a replacement for them — both are populated from the same
measurements.

**A previously-invisible failure mode is now visible.** Before this
version, a RAG or Memory retrieval failure degraded gracefully (see
`docs/RAG.md`/`docs/MEMORY.md`) but left **no trace** of having
happened — the top-level `error` column stayed `NULL` because the
Kernel didn't crash. `context_engine.py`'s `assemble()` now reports
`memory_status`/`rag_status` per source; `kernel.py` reflects both into
their own `steps` entries and downgrades the overall run `status` to
`'partial'` when either failed. An "everything looks fine" trace and a
"we quietly served a degraded answer" trace are no longer
indistinguishable.

**`failed_step` localization, on a genuine (non-degraded) failure:** a
`last_step_attempted` variable is set immediately before each risky
call in `kernel.py`, so if the Kernel's outer exception handler fires,
`failed_step` names the exact stage that was in flight — no guessing
from a generic 500-equivalent.

**Security: no secrets in traces.** Per-step error text (a new kind of
surface area — the *existing* top-level `error` column was already
safe, being just a static label like `"kernel_fallback"`) is passed
through `app/core/trace_sanitize.py`'s `safe_error_message()` before
it ever reaches `steps` or `AgentRunTrace`. It redacts DSN-embedded
credentials (`postgresql://user:pass@host`), `Authorization`/`Bearer`
values, `api_key=`/`password=`-style fields, and Anthropic/OpenAI-style
`sk-...` tokens, then truncates to 300 characters. See
`tests/test_trace_sanitize.py` for the exact cases covered — a real bug
here (the `Authorization`/`Bearer` patterns initially being combined
into one alternation, which let `Authorization:` swallow just the
word "Bearer" and leave the actual token exposed) was caught by these
tests during this phase and fixed before release, not after.

## Sibling foundation modules (defined, tested, NOT wired into CoreAgent/Kernel)

Three more capabilities were built to the same "foundation only" scope
as Tenant Brain below — complete, independently unit-tested, but with
no production caller yet, per the task's rule 5 ("do not activate
future behavior unless explicitly implemented and tested" — these are
implemented and tested in isolation, but activating them inside
Kernel/CoreAgent would be a real behavior change with no consumer yet
to justify it):

- **Capability Registry** (`app/capabilities/`) — `CapabilityDefinition`
  (name, description, input_schema, requires_confirmation, enabled,
  optional handler) + `CapabilityRegistry` (register/get/list). Starts
  empty; no real tool is registered (none existed before this task).
  Future home: the Tool Engine (`docs/ROADMAP.md` item 3) reads from
  this registry instead of hardcoding tool logic.
- **Goal / Outcome** (`app/goal/goal_types.py`) — `GoalSignal` +
  `infer_goal_signal(intent: IntentType) -> GoalSignal`, a pure,
  side-effect-free static mapping (e.g. `CREATE_ORDER` →
  `"complete_purchase"`). Never overrides Intent or business rules —
  it's an additional signal, not a decision. Not called from `kernel.py`.
- **Identity Resolution** — lives in `node-api`, not here (customer
  identity is persistence/business data, architecture rule 4). See
  `docs/ARCHITECTURE.md`.

## `AgentConfig` and `TenantBrain` — loaded per-request, still not consumed by behavior

```python
class CoreAgent:
    def __init__(self, config: AgentConfig = DEFAULT_AGENT_CONFIG):
        self._fallback_config = config   # only used if the DB load below fails

    async def run(self, db, request):
        try:
            tenant_config = await load_agent_config(db, request.tenantId)
        except Exception:
            logger.error(...)
            tenant_config = self._fallback_config
        logger.info(..., tenant_config.vertical, tenant_config.tools_enabled)
        ...
```

`load_agent_config()` (`app/agent/agent_config.py`) now runs a real
`SELECT vertical, config FROM agent_configs WHERE tenant_id = :tenant_id`
on every call, falling back to `AgentConfig()` defaults if no row
exists for that tenant. This is a genuine fix from "constructed once,
never read" — the value is real per-tenant data, logged for
observability.

**Important concurrency note:** `core_agent = CoreAgent()` is a
module-level singleton shared across all concurrent requests for every
tenant. `tenant_config` is therefore always a local variable inside
`run()`, never assigned back onto `self` — doing so would let one
tenant's config leak into another tenant's in-flight request.
`self._fallback_config` is fixed at construction and only used as an
error fallback, never mutated.

**Still not consumed downstream.** Nothing in the Kernel branches on
`tenant_config.vertical` or `tenant_config.tools_enabled` yet — there's
no Tool Engine to gate, and no per-vertical prompt/behavior switch
exists. This is real wiring, not yet real behavior change. See
`docs/ROADMAP.md` item 3 for what will actually consume it.

### `TenantBrain` — composes AgentConfig + business_rules, not called yet

`app/agent/tenant_brain.py` defines `TenantBrain` (a tenant's
`AgentConfig` + its active `business_rules`) and
`load_tenant_brain(db, tenant_id)`, which reuses the existing
`load_agent_config()` (no duplicate query) and adds one new read
against `business_rules` (the table existed already; nothing had ever
read it before this). Implements the extension spec's core principle
literally:

```
Shared Core Agent + Tenant-specific configuration/knowledge/business rules
= Tenant-specific AI behavior
```

**Deliberately not called from `CoreAgent.run()`.** Doing so would add
a `business_rules` query to every single message for data nothing
currently consumes (no Decision Engine exists to apply a rule to
anything) — a real per-request cost increase with zero behavior
benefit today. `TenantBrain` is complete and independently tested
(see `python-api/tests/test_agent_foundation.py`); a future Decision
Engine can call `load_tenant_brain()` directly once it exists.

## Contract stability (why `AgentRequest`/`AgentResponse` exist)

`AgentRequest`/`AgentResponse` are a strict superset of the older
`KernelRunRequest`/`KernelRunResponse` (`schemas/kernel.py`) — same
required fields, only new fields added with defaults
(`channel`, `metadata`, `requestId` on the request;
`metadata` on the response). This was a deliberate non-breaking change:
`node-api`'s `ai.client.ts` sends the same JSON shape it always did and
still validates against `AgentRequest` correctly.

`KernelRunResponse` (still used internally, as `Kernel.run()`'s own
return type) additionally carries `detectedLanguage`/`replyLanguage`,
and — new this version — `normalizedMessage`, `decision`,
`retrievalUsed`, `retrievalChunkCount`, `errorOccurred`. All optional
with defaults, all additive. These exist so `CoreAgent` can build a
meaningful `AgentRunTrace` (see above) without the Kernel's own control
flow, prompts, or error boundaries changing at all.

## What Core Agent is NOT (by design, per the original integration spec)

- Not a duplicate of RAG/Memory/Intent/State/Context — it delegates
  100% of that to `kernel.run()`.
- Not where Tool calling, Decision-making, or Negotiation live — those
  don't exist yet, and when built they belong in the Kernel (or a new
  engine the Kernel calls), not in `CoreAgent`.
- Not a separate microservice — it's a plain Python class inside
  `python-api`, called in-process by the FastAPI route handler.
