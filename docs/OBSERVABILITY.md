# docs/OBSERVABILITY.md — Phase 1: Observability Foundation

Legend: ✅ IMPLEMENTED · 🟡 PARTIALLY IMPLEMENTED · ⬜ PLANNED

This is a **foundation**, not a monitoring platform. No Prometheus, no
Grafana, no Kubernetes, no autoscaling, no alerting — those are later
phases. Everything here is either an in-memory counter, a plain JSON
endpoint, a structured log line, or a column on the existing
`agent_run_traces` table. Nothing new was introduced as a dependency
(no `prom-client`, no `psutil`) — both services' request/infra metrics
are built from stdlib/built-ins only.

## 1. Request metrics — ✅ IMPLEMENTED (both services)

| Field | node-api | python-api |
|---|---|---|
| Request count | `utils/metrics.ts` → `recordRequest()` | `core/metrics.py` → `record_request()` |
| Request duration | same | same |
| HTTP status | tracked per-code in `statusCounts` | tracked per-code in `status_counts` |
| Error count (5xx) | `errorCount` | `error_count` |
| request_id | logged on every request line | logged on every request line |

Wired via middleware that wraps **every** route, not opt-in per
handler: `node-api/src/middleware/metrics.ts` (mounted in `app.ts`
right after `requestId`), `python-api/app/core/request_middleware.py`
(`RequestMetricsMiddleware`, added first in `main.py` so it wraps
every route below it). Both log one structured `http_request` line per
request and expose the running totals at `GET /metrics`.

## 2. AI/Kernel metrics — ✅ IMPLEMENTED (measurement), 🟡 (not yet consumed beyond the trace)

Every step in `python-api/app/kernel/kernel.py` is individually timed
with `time.perf_counter()`, no new calls added — timing wraps the
exact calls the Kernel already made:

| Metric | Measured in | Kernel field | Trace column |
|---|---|---|---|
| Language latency | `kernel.py` step 1 | `languageLatencyMs` | `language_latency_ms` |
| Intent latency | `kernel.py` step 2 | `intentLatencyMs` | `intent_latency_ms` |
| Context latency | `kernel.py` step 5 (wraps the whole `assemble()` call) | `contextLatencyMs` | `context_latency_ms` |
| Memory retrieval latency | `context_engine.py`'s `assemble()`, the `memory_search.get_all()` call specifically | `memoryLatencyMs` | `memory_latency_ms` |
| RAG latency | `context_engine.py`'s `assemble()`, the `hybrid_search.search()` call specifically | `ragLatencyMs` | `rag_latency_ms` |
| LLM latency | `kernel.py`, the final `ai_service.complete()` call (either branch) | `llmLatencyMs` | `llm_latency_ms` |
| Total Kernel latency | `kernel.py`, wraps the entire `run()` body | `kernelLatencyMs` | *(rolls into `agent_run_traces.latency_ms`, see below)* |
| Total Agent Run latency | `core_agent.py`, wraps `kernel.run()` + trace write | *(not a Kernel field — measured one level up)* | `agent_run_traces.latency_ms` |

**Not every step runs on every turn**: on the low-confidence
"clarify" path, `context`/`memory`/`rag` are skipped entirely (see
`docs/KERNEL.md`), so those three stay `None`/`NULL` for that turn —
this is correct, not a bug; a step that didn't run has no latency to
report.

**Where this data ends up:** `CoreAgent.run()` reads all of these off
`KernelRunResponse` and writes them into one `agent_run_traces` row per
run (`app/trace/trace_service.py`) — see section 8 below. There is
**no separate metrics aggregation** for these yet (e.g. "average LLM
latency this week") — query `agent_run_traces` directly with SQL.

## 3. Infrastructure metrics — ✅ IMPLEMENTED (foundation only)

"Safest minimal way", per the task: **no new dependency**, in-memory
only, read from what the runtime already exposes.

| Metric | node-api (`utils/metrics.ts` → `getInfraSnapshot()`) | python-api (`core/metrics.py` → `get_infra_snapshot()`) |
|---|---|---|
| CPU usage | `process.cpuUsage()` — **cumulative** user+system seconds since start, not a % | `resource.getrusage()` — same caveat |
| RAM usage | `process.memoryUsage().rss`, in MB | `/proc/self/status` `VmRSS`, falls back to `ru_maxrss` if `/proc` is unavailable |
| Process uptime | `Date.now() - processStartedAt` | `time.time() - _process_started_at` |
| Basic health | `GET /health` (both services) | same |

**Important caveat, stated explicitly so it's never misread later:**
`cpuTimeSeconds`/`cpu_time_seconds` is **cumulative CPU time consumed
since the process started**, not an instantaneous CPU percentage.
Computing a true CPU% needs sampling over an interval (e.g. two reads
a second apart) — out of scope for this phase. If a future phase wants
"CPU % right now", that's a new, deliberate addition, not something
already secretly here.

**Single-process assumption:** both services run as one process each
(see `docker-compose.yml` — no cluster mode, no multiple uvicorn
workers). These counters are correct for that. If workers/replicas are
ever added, in-memory counters become **per-instance**, not global —
flagged in both `metrics.py`/`metrics.ts` source comments too.

## 4. Database health — ✅ IMPLEMENTED (both services)

| | node-api | python-api |
|---|---|---|
| Function | `checkDatabaseHealth()` in `config/database.ts` | `check_database_health()` in `core/database.py` |
| Query cost | `SELECT 1` — no table scan | `SELECT 1` — no table scan |
| Pool stats | `pool.totalCount`/`idleCount`/`waitingCount` (already tracked by `pg`, free to read) | `engine.pool.size()`/`checkedout()` (already tracked by SQLAlchemy, free to read) |
| Used by | `GET /readiness` only — **never** on any request-serving path | same |

Neither service runs this on a hot path — it's readiness-only, exactly
per the "do not run expensive database queries" constraint (and
`SELECT 1` costs nothing expensive even if it were).

## 5. Redis health — ✅ node-api, 🟡 python-api (has no Redis dependency to check)

- **node-api** (`checkRedisHealth()` in `config/redis.ts`): `PING` →
  expects `PONG`. Used by `GET /readiness`. node-api genuinely
  *depends* on Redis (rate limiting, product-list caching — see
  `PROJECT_STATUS.md`), so this is a real readiness signal.
- **python-api**: `redis` is in `requirements.txt` but **never
  imported/used anywhere** in this service (confirmed in
  `PROJECT_STATUS.md` before this phase, unchanged by it). Its
  `GET /readiness` deliberately does **not** fabricate a Redis check
  for a dependency it doesn't actually have — see the docstring in
  `python-api/app/api/routes/readiness.py`. If python-api ever adopts
  Redis for something, add the check there at that time.

## 6. Health endpoints — ✅ IMPLEMENTED (both services), clearly separated

| | `GET /health` | `GET /readiness` |
|---|---|---|
| Question answered | Is the process alive? | Can this service currently serve a request? |
| Checks dependencies? | **No** — zero DB/Redis calls | **Yes** — DB (+ Redis on node-api) |
| Auth | None (both services) | node-api: none · python-api: `x-internal-secret` (see below) |
| Status codes | always `200` | `200` if ready, `503` if not |
| Used by | Docker's own `healthcheck:` (`docker-compose.yml`) | Would gate a load balancer/orchestrator later — no such consumer exists yet in this phase |

**Auth asymmetry, deliberate and disclosed, not an oversight:**
python-api's own pre-existing rule is "every route requires
`x-internal-secret` except `/health`" (see `docs/API_CONTRACTS.md`,
predates this phase) — `/readiness` and `/metrics` follow that rule to
stay consistent with an already-established contract, rather than
carve out a silent new exception. node-api has no equivalent blanket
rule (its auth is per-route-group: `x-api-key` for channels, JWT for
dashboard) — so for node-api, `/health` and `/readiness` are public
(matches how orchestration tooling conventionally polls these — it
won't have tenant/dashboard credentials to present), while `/metrics`
sits behind `requireAuth` (JWT) since aggregate request-volume/error
data is more appropriate for staff-only access and nothing external
needs to poll it.

`GET /metrics` (plain JSON, not Prometheus format) exists on both
services as a small bonus beyond the task's literal ask, since the
counters from section 1 needed *some* way to be read back — see each
service's `api/routes/metrics.py` / `modules/health/health.routes.ts`.

## 7. Structured logging — ✅ IMPLEMENTED (format), 🟡 (not every field on every line)

| | node-api | python-api |
|---|---|---|
| Format | JSON, one line per log call (`utils/logger.ts`) | JSON, one line per log call (`core/logging.py`'s `JsonFormatter`) |
| Applied to | `ai.client.ts`, `errorHandler.ts`, the new `metrics.ts` middleware | **every** existing `logger.info`/`logger.error` call, automatically — `JsonFormatter` wraps the *existing* text-formatted calls without requiring every call site in `kernel.py`/`ai_service.py`/etc. to change |
| Can carry | `requestId`, `tenantId`, `customerId`, `conversationId`, `messageId`, `agentRunId`, `latencyMs`, `error`, `category` — as `extra`/object fields, not string interpolation | same fields, passed via Python logging's `extra={...}` kwarg |

**Not yet unified across services**: node-api's JSON keys are
camelCase, python-api's are snake_case (matching each language's own
convention elsewhere in this codebase) — a log aggregator would need
to normalize both, not query one shared key name across services. This
was a pre-existing gap noted in `PROJECT_STATUS.md` before this phase
and is **not** fixed here (out of scope — would mean renaming fields
across a lot of existing call sites for a cross-cutting concern, more
than "foundation").

**What is never logged, enforced by discipline (not redaction) — no
existing call site does this, verified by search before this phase and
kept true after it:**
- API keys (`x-api-key`, `access_token`, WhatsApp tokens)
- Passwords
- JWT secrets / `Authorization` header contents
- `INTERNAL_SERVICE_SECRET` / `x-internal-secret`
- Full customer message content in routine logs (Kernel logs *metadata
  about* a message — intent, confidence, language — never the message
  text itself in its own dedicated log line; the one place a message
  travels is inside the LLM prompt itself, which isn't logged)

If you add a new log call, do not pass a request body, a header
object, or a raw credential into it — pass the specific fields you
need (ids, counts, categories), the same way every existing call site
already does.

## 8. Agent Run Trace integration — ✅ IMPLEMENTED, reusing the existing trace (no second system built)

**Explicitly reused, not duplicated**: `agent_run_traces`
(`db/init/005_agent_foundation.sql`, first built for the Agent
Foundation Layer) is the *same* table this phase extends —
`db/init/007_observability.sql` only `ALTER TABLE`s it to add seven
columns (six per-step latencies + `error_category`). `CoreAgent.run()`
was already calling `record_trace()` once per run before this phase;
this phase did not add a second call site or a second table.

**Correlation fields present on every row**, exactly as required:
`agent_run_id`, `request_id`, `tenant_id`, `customer_id`,
`conversation_id`, `message_id` — all six on the same row, so any one
of them can be used to find the others with a single `SELECT`.

**Bug found and fixed during this phase**: the per-step latency
fields were computed by the Kernel and present on
`KernelRunResponse`, but `AgentRunTrace` (`trace_types.py`) and the
`INSERT` in `trace_service.py` were never updated to include them —
the data was computed and then silently discarded before reaching the
database. Fixed in this phase; a regression test now pins it
(`test_record_trace_includes_per_step_latency_and_error_category` in
`python-api/tests/test_agent_foundation.py`).

**Still write-only** — no read/query API for `agent_run_traces`
exists (unchanged from before this phase; "do not build a complete
analytics platform yet" applies here too). Query it directly:

**Extended further in a later phase** (Commercial V1 lifecycle
hardening — `db/init/008_trace_lifecycle.sql`): `trace_id`, `channel`,
`status`, `failed_step`, and a `steps` JSONB array complete the
correlation chain and give per-stage status/duration/error visibility,
including making previously-invisible RAG/Memory degradation visible
for the first time. Full detail in `docs/AGENT.md`'s "Commercial V1
lifecycle hardening" section and `docs/CONTEXT_ENGINE.md`.

```sql
SELECT agent_run_id, request_id, intent, confidence, decision,
       language_latency_ms, intent_latency_ms, context_latency_ms,
       memory_latency_ms, rag_latency_ms, llm_latency_ms, latency_ms,
       error_category
  FROM agent_run_traces
 WHERE tenant_id = 1
 ORDER BY created_at DESC
 LIMIT 20;
```

## 9. Error classification — ✅ IMPLEMENTED (taxonomy + wiring), same 8 categories on both services

`ErrorCategory`: `api_error`, `database_error`, `redis_error`,
`llm_error`, `rag_error`, `memory_error`, `timeout`, `validation_error`.

- **python-api**: `app/core/error_types.py` — `classify_exception()`.
  Wired into `kernel.py`'s outer fallback handler (the *only* place in
  the Kernel where an uncaught exception is turned into a customer
  reply) — every fallback now carries a category, both in the log line
  and in `KernelRunResponse.errorCategory` → `agent_run_traces.error_category`.
- **node-api**: `src/utils/errorCategory.ts` — `classifyError()`, same
  string values by hand-kept convention (see the file's docstring for
  why an enum can't be shared directly across languages). Wired into
  `middleware/errorHandler.ts`, which now logs every error (both
  `AppError` and unhandled) with its category instead of a raw
  `console.error`.

**This does not change how any existing `try`/`except`/`catch`
handles an error** — every control-flow decision already documented in
`docs/KERNEL.md`'s error-boundary table is unchanged. Classification
is purely an additional label attached when an error is already being
logged, for searchability (`grep error_category=llm_error` across
logs) and for the trace column.

**RAG/Memory errors specifically**: `context_engine.py`'s own isolated
try/except blocks (see `docs/RAG.md`/`docs/MEMORY.md`) still swallow
RAG/Memory failures internally and don't reach `kernel.py`'s outer
handler at all in the normal case — so `rag_error`/`memory_error`
labels are reachable in principle (the taxonomy exists for them) but
in practice today's isolated-recovery design means most RAG/Memory
failures never produce a *classified* error line, because they're
handled quietly rather than raised. This is consistent with the
existing "controlled failure" design (`docs/KERNEL.md`), not a gap
introduced by this phase.

## 10. Documentation — this file + four updates

This file is new. Also updated in this phase: `AI_CONTEXT.md`,
`PROJECT_STATUS.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md` — see
each for the specific IMPLEMENTED/PARTIAL/PLANNED entries they now
carry for Phase 1.

## Explicitly NOT built in this phase (later phases)

⬜ Load testing · ⬜ Automatic alerting · ⬜ Prometheus/OpenMetrics
format · ⬜ Grafana or any dashboard · ⬜ Kubernetes manifests/probes ·
⬜ Autoscaling · ⬜ Cloud infrastructure (this stays local-Docker,
per `docker-compose.yml`, unchanged in shape) · ⬜ Cross-service log
field unification (camelCase vs snake_case, section 7) · ⬜ A read/query
API for `agent_run_traces` · ⬜ True instantaneous CPU percentage
(section 3's cumulative-time caveat) · ⬜ Cost tracking
(`agent_run_traces.cost_usd` stays `NULL` — no per-model pricing table
exists; unrelated to this phase, tracked in `docs/ROADMAP.md`)

## Files touched or added in this phase

**New:**
`db/init/007_observability.sql` ·
`python-api/app/core/metrics.py` · `request_middleware.py` · `error_types.py` ·
`python-api/app/api/routes/readiness.py` · `metrics.py` ·
`node-api/src/utils/metrics.ts` · `errorCategory.ts` (+ both `.test.ts`) ·
`node-api/src/middleware/metrics.ts` ·
`node-api/src/modules/health/health.routes.ts` ·
`python-api/tests/test_observability.py` (18 tests: `classify_exception()` + `metrics.py`) ·
`docs/OBSERVABILITY.md` (this file)

**Modified:**
`python-api/app/kernel/kernel.py` (per-step timing + error classification) ·
`python-api/app/context/context_engine.py` (memory/RAG timing) ·
`python-api/app/schemas/kernel.py` (new optional latency/error fields) ·
`python-api/app/agent/core_agent.py` (agent_run_id generation, trace now carries all new fields — bug fixed here) ·
`python-api/app/trace/trace_types.py` · `trace_service.py` (the fix itself) ·
`python-api/app/core/database.py` (`check_database_health`) ·
`python-api/app/core/logging.py` (JSON formatter) ·
`python-api/app/main.py` (middleware + route registration) ·
`node-api/src/config/database.ts` (`checkDatabaseHealth`) · `redis.ts` (`checkRedisHealth`) ·
`node-api/src/middleware/errorHandler.ts` (classification + structured logging) ·
`node-api/src/app.ts` (routing + middleware wiring) ·
`docker-compose.yml` (healthcheck blocks only — no new services, no Prometheus/Grafana/K8s)
