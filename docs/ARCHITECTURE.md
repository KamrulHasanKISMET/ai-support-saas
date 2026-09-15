# docs/ARCHITECTURE.md

## Services (docker-compose.yml)

| Service | Image/build | Port | Role |
|---|---|---|---|
| `postgres` | `pgvector/pgvector:pg16` | 5432 | Single source of truth. Schema loaded from `db/init/*.sql` in filename order, **only on first boot of an empty volume** |
| `redis` | `redis:7-alpine` | 6379 | Cache, rate limiting. NOT durable state |
| `python-api` | `./python-api` (FastAPI, `uvicorn --reload`) | 8000 | AI Service — Core Agent, Kernel, all engines |
| `node-api` | `./node-api` (Express/TS, `npm run dev`) | 4000 | Business backend — the only thing customers/channels/dashboard talk to |

No frontend service exists in docker-compose. **Docker itself is still
not "rebuilt"** in the sense `AI_CONTEXT.md`'s golden rule means (no
new services, no orchestration platform) — the one change made to
`docker-compose.yml` across this project's history is a `healthcheck:`
block on `python-api`/`node-api` (Phase 1 observability, see
`docs/OBSERVABILITY.md`), each polling that service's own `GET /health`.
`postgres`/`redis` already had healthchecks; the app services didn't
until that phase.

## End-to-end data flow (a customer message)

```
Customer (website/WhatsApp/Facebook)
   ↓
Channel Adapter (website: direct API call; WhatsApp: real webhook,
   see "WhatsApp channel" below; Facebook: still a TODO stub)
   ↓
node-api  POST /messages   [requireApiKey + rate limit]
   │   - resolve/create conversation
   │   - store the customer's raw message
   ↓
node-api  ai.client.ts → POST python-api /ai/kernel/run
   ↓
python-api  app/api/routes/ai.py
   ↓
CoreAgent.run()  (app/agent/core_agent.py — thin wrapper, see docs/AGENT.md)
   │   - loads AgentConfig (agent_configs table)
   ↓
Kernel.run()  (app/kernel/kernel.py — see docs/KERNEL.md)
   ↓
Language Engine → Intent Engine → State Engine → Context Engine
   (Memory read + RAG search) → LLM (Claude) → Memory write
   │   - every step above is individually timed (Phase 1 observability,
   │     see docs/OBSERVABILITY.md)
   ↓
KernelRunResponse
   │   - CoreAgent records one AgentRunTrace row, now including the
   │     per-step latencies + an error category (app/trace/ — isolated,
   │     never blocks the reply) — see "Agent Foundation Layer" below
   ↓
AgentResponse → node-api
   │   - store the AI's reply
   ↓
Customer receives the reply
```

Node.js never runs AI logic itself; Python never touches
customer-facing routing/auth. This separation is intentional and load-
bearing — do not blur it when adding features.

## Agent Foundation Layer

Architectural foundations for five future capabilities (Tenant Brain,
Identity Resolution, Capability Registry, Goal/Outcome, Agent Run
Trace) were added as an extension layer on top of the existing
Core Agent/Kernel — none of them replace or duplicate Core Agent,
Kernel, RAG, Memory, Context, Intent, or State. Full detail in
`docs/AGENT.md` and `PROJECT_STATUS.md`; summary:

| Capability | Lives in | Wired into production flow? |
|---|---|---|
| Tenant Brain | `python-api/app/agent/tenant_brain.py` | No — composes existing `agent_configs` + `business_rules`, standalone |
| Identity Resolution | `node-api/src/modules/customers/identity_resolution.service.ts` | **Yes** — called from the WhatsApp webhook (`whatsapp.webhook.ts`) to resolve/create the customer for each inbound message, deterministic exact-match only (channel → phone → email) |
| Capability Registry | `python-api/app/capabilities/` | No — starts empty, no real tool registered |
| Goal / Outcome | `python-api/app/goal/goal_types.py` | No — pure `Intent -> GoalSignal` mapping, not read by the Kernel |
| Agent Run Trace | `python-api/app/trace/` + `agent_run_traces` table | **Yes** — `CoreAgent.run()` writes one row per run, isolated so a trace failure can't affect the reply |

Agent Run Trace and Identity Resolution are the two active capabilities
in this version. Tenant Brain, Capability Registry, and Goal/Outcome
remain complete, independently unit-tested modules with no production
caller — each was kept deliberately unwired because the natural
consumer (Decision Engine, Tool Engine) doesn't exist yet; wiring them
in now would mean guessing at an integration point rather than
building a real one.

## WhatsApp channel (new)

A real, working channel — not a stub. `POST /webhooks/whatsapp` (Meta
calls this directly; see `docs/API_CONTRACTS.md`) → verify
`X-Hub-Signature-256` → parse the payload → de-duplicate via Redis →
look up the owning tenant (`channel_credentials`, new table) → resolve
or create the customer (Identity Resolution, above) → the SAME
`processIncomingMessage()` function `POST /messages` uses (extracted
into `messages.service.ts` so there is only one Channel → Kernel →
Reply pipeline, not two drifting copies) → send the reply back via the
WhatsApp Cloud API. Full detail, including exactly what is and isn't
covered by automated tests, in `PROJECT_STATUS.md`'s "WhatsApp channel"
section.

## Directory structure (as it actually exists)

```
ai-support-saas/
├── AI_CONTEXT.md              ← read first
├── PROJECT_STATUS.md          ← implemented/partial/planned
├── docs/                       ← this folder
├── docker-compose.yml
├── db/init/
│   ├── 001_extensions.sql      pgvector, pgcrypto
│   ├── 002_core_tables.sql     tenants, users, customers, conversations,
│   │                            messages, products, orders, business_rules,
│   │                            agent_configs, customer_channel_identities
│   ├── 003_knowledge_rag.sql   knowledge_documents/chunks, customer_memories,
│   │                            conversation_states
│   ├── 004_auth_billing.sql    tenants.api_key, subscriptions
│   ├── 005_agent_foundation.sql agent_run_traces (Agent Run Trace)
│   ├── 006_channel_credentials.sql channel_credentials (WhatsApp/Facebook tokens)
│   └── 007_observability.sql   agent_run_traces += per-step latency columns +
│                                error_category (Phase 1, see docs/OBSERVABILITY.md)
├── node-api/
│   ├── public/                   connect-whatsapp.html — minimal static
│   │                             settings page (no build step), served
│   │                             directly by express.static in app.ts
│   └── src/
│       ├── app.ts               route wiring + middleware order (read this
│       │                        to see which auth each route needs)
│       ├── server.ts             listen + graceful shutdown
│       ├── config/               env, postgres pool (+ checkDatabaseHealth),
│       │                        redis client (+ checkRedisHealth)
│       ├── middleware/
│       │   ├── auth.ts            requireApiKey, requireAuth
│       │   ├── tenant.ts          TenantRequest type; deprecated resolveTenant
│       │   ├── rateLimiter.ts     Redis-backed fixed-window limiter
│       │   ├── requestId.ts       x-request-id assignment/propagation
│       │   ├── metrics.ts         Phase 1: times every request, records +
│       │   │                     logs it (see docs/OBSERVABILITY.md)
│       │   ├── rawBody.ts         stashes raw bytes for WhatsApp signature verification
│       │   └── errorHandler.ts    AppError → JSON + classified structured
│       │                         log line; unhandled → generic 500
│       ├── utils/                 crypto (api key gen), logger (JSON),
│       │                        metrics.ts (Phase 1 counters/infra snapshot),
│       │                        errorCategory.ts (Phase 1 taxonomy, + tests)
│       └── modules/               one folder per resource: auth, tenants,
│                                   customers, conversations, messages,
│                                   channels, products, orders, billing, ai,
│                                   health (Phase 1: /health, /readiness, /metrics)
│                                   (ai/ai.client.ts = the only bridge to python-api)
│           ├── messages/          messages.routes.ts (thin wrapper),
│           │                      messages.service.ts (processIncomingMessage —
│           │                      the ONE Channel->Kernel->Reply implementation)
│           ├── customers/         customers.repository.ts,
│           │                      identity_resolution.service.ts (+ test)
│           ├── health/            health.routes.ts — healthRouter, readinessRouter,
│           │                      metricsRouter (Phase 1, see docs/OBSERVABILITY.md)
│           └── channels/          channels.routes.ts (credential management, JWT),
│                                  whatsapp.webhook.ts (the actual Meta-facing
│                                  webhook — public, signature-authed),
│                                  whatsapp.signature.ts (+ test),
│                                  whatsapp.parser.ts (+ test), whatsapp.client.ts
│                                  (outbound send), channel_credentials.repository.ts
├── python-api/
│   └── app/
│       ├── main.py                FastAPI app, RequestMetricsMiddleware +
│       │                        router registration
│       ├── core/                  config (Settings), database (async engine +
│       │                        check_database_health), logging (JSON formatter),
│       │                        security (require_internal_secret),
│       │                        metrics.py (Phase 1 counters/infra snapshot),
│       │                        request_middleware.py (Phase 1 per-request timing),
│       │                        error_types.py (Phase 1 error taxonomy)
│       ├── api/routes/            ai.py (Kernel entrypoint), rag.py, memory.py,
│       │                          health.py, readiness.py, metrics.py (Phase 1 —
│       │                          rag.py/memory.py/readiness.py/metrics.py all
│       │                          require x-internal-secret; health.py doesn't)
│       ├── agent/                 core_agent.py, agent_config.py, tenant_brain.py
│       │                          (see docs/AGENT.md)
│       ├── trace/                 trace_types.py, trace_service.py — Agent Run Trace
│       │                          (wired: CoreAgent writes one row per run, now
│       │                          including Phase 1's per-step latency + error
│       │                          category — see docs/OBSERVABILITY.md)
│       ├── capabilities/          capability_types.py, capability_registry.py —
│       │                          Capability Registry foundation (empty, unwired)
│       ├── goal/                  goal_types.py — Goal/Outcome foundation (unwired)
│       ├── kernel/kernel.py       orchestrator (see docs/KERNEL.md)
│       ├── language/              language_engine.py, language_types.py
│       ├── intent/                intent_engine.py, intent_types.py
│       ├── state/                 state_engine.py, state_types.py
│       ├── context/context_engine.py
│       ├── memory/                memory_service.py (write), memory_search.py (read)
│       ├── rag/                   search.py, hybrid_search.py, reranker.py, context_builder.py
│       ├── ai/                    ai_service.py (LLM), embedding_service.py (NOT implemented)
│       └── schemas/                agent.py (AgentRequest/Response), kernel.py (KernelRunRequest/Response)
└── web/                          empty scaffold only — no frontend built
```

Node.js's `node-api/src/modules/customers/` additionally has
`identity_resolution.service.ts` (+ its test) — now called from the
WhatsApp webhook (see "WhatsApp channel" above), not just a standalone
foundation anymore.

## Core architectural rules (still true, still enforced)

1. Every tenant-scoped query filters by `tenant_id` — no exceptions.
2. The LLM never writes to the database directly. It proposes
   (Intent entities, Memory candidates); application code validates
   and writes.
3. Language / Intent / State / Context / Memory / RAG stay in separate
   modules even though the Kernel orchestrates all of them.
4. Node.js does routing/persistence/auth/billing. Python does
   reasoning. Neither crosses into the other's job.
5. New capability (Tools, Decision Engine, Negotiation) extends the
   Kernel/Core Agent — it does not get its own parallel pipeline.
6. A capability can be built as an architectural foundation (schema +
   interface + minimal code, independently tested) without being wired
   into the Kernel/CoreAgent's actual decision flow — see "Agent
   Foundation Layer" above. Wire it in only once a real consumer needs
   it; don't guess at an integration point that doesn't exist yet.
