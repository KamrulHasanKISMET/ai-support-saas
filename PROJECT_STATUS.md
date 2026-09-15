# PROJECT_STATUS.md

Verified against the actual code on the date this was written. "Verified"
means someone traced the actual call path, not just read a comment.

Legend: ✅ IMPLEMENTED · 🟡 PARTIAL (code exists but incomplete/unwired/dead) · ⬜ PLANNED (not started)

## Multi-tenancy & data model

| Item | Status | Notes |
|---|---|---|
| `tenant_id` on every tenant-scoped table | ✅ | `db/init/002_core_tables.sql`, `003_knowledge_rag.sql` |
| Tenant isolation enforced in queries | ✅ | Every repository/engine query filters by `tenant_id` |
| Cross-channel unified customer identity | 🟡 | `customer_channel_identities` table + `findByChannelIdentity`/`linkChannelIdentity` repository methods, **plus new** `findByPhone`/`findByEmail` (exact-match only) and a formal `identity_resolution.service.ts` (deterministic priority: channel → phone → email → none — see `docs/ARCHITECTURE.md`). Fully unit-tested (8 tests). **Still not called from any route** — the natural caller is a channel webhook handler, which doesn't exist yet |
| `business_rules` table | 🟡 | Now read (for the first time) by `app/agent/tenant_brain.py`'s `load_tenant_brain()` — but that function is standalone and **not called from Kernel/CoreAgent** yet, so this table still has no production consumer |
| `agent_configs` table | 🟡 | Loaded per-request by `CoreAgent.run()` via `load_agent_config()` (unchanged from before). **Now also** composed into `TenantBrain` (`app/agent/tenant_brain.py`), which isn't wired into the running Kernel/CoreAgent yet — see the Agent Foundation Layer section below |
| `conversations.status` lifecycle (open/closed/escalated) | 🟡 | Set to `'open'` on creation, **never updated** — no code closes or escalates a conversation |

## Agent Foundation Layer (new)

Architectural foundations for 5 future capabilities. Only Agent Run
Trace is actually wired into the running system — the other four are
complete, independently tested modules with **no production caller
yet** (deliberate — see docs/AGENT.md for why each one stayed
unwired). Full detail in `docs/AGENT.md` and `docs/ARCHITECTURE.md`.

| Capability | Status | Where |
|---|---|---|
| Tenant Brain | 🟡 foundation only | `app/agent/tenant_brain.py` — composes existing `AgentConfig` + `business_rules` (both pre-existing tables). Not called from CoreAgent/Kernel. |
| Identity Resolution | ✅ wired | `node-api/src/modules/customers/identity_resolution.service.ts` — deterministic exact-match only (channel → phone → email). **Now called** from the WhatsApp webhook (`whatsapp.webhook.ts`) to resolve/create the customer for each inbound message. |
| Capability Registry | 🟡 foundation only | `app/capabilities/` — in-memory register/get/list contract. Starts empty; no real tool is registered; never called by the Kernel. |
| Goal / Outcome | 🟡 foundation only | `app/goal/goal_types.py` — pure `IntentType -> GoalSignal` mapping function. Not called from the Kernel. |
| Agent Run Trace | ✅ wired and tested | `app/trace/` + `agent_run_traces` table. `CoreAgent.run()` calls `record_trace()` after every Kernel run (isolated try/except — a trace-write failure never affects the reply). Write-only; no read/query API yet. |

## Auth

| Item | Status | Notes |
|---|---|---|
| Per-tenant API key (`x-api-key`) for channel/service routes | ✅ | `middleware/auth.ts` → `requireApiKey`, verified against `tenants.api_key` |
| JWT (`Authorization: Bearer`) for dashboard routes | ✅ | `middleware/auth.ts` → `requireAuth` |
| Signup (creates tenant + owner user + api_key in one transaction) | ✅ | `POST /auth/signup` |
| Login | ✅ | `POST /auth/login`, scoped by `tenantSlug` (emails unique per-tenant, not globally) |
| Password hashing | ✅ | bcryptjs, cost 12 |
| Old insecure `x-tenant-id` header trust | ✅ removed | `resolveTenant` in `middleware/tenant.ts` kept only as a marked `@deprecated` reference; nothing calls it |
| Role-based permissions (owner vs staff) | ⬜ | `role` column exists on `users`/JWT claims, nothing checks it yet |
| `GET /tenants/:slug` leaking `api_key` | ✅ fixed | Was returning the full row including `api_key` to any unauthenticated caller; now strips it |
| Service-to-service auth between node-api and python-api | ✅ fixed | `x-internal-secret` header, checked by `require_internal_secret` FastAPI dependency on every `python-api` route except `/health`. Fails **closed** if `INTERNAL_SERVICE_SECRET` is unset. `node-api` sends it on every call in `ai.client.ts`. Both services must share the same value — set once in root `.env`, which `docker-compose.yml` passes to both. |

## Node.js — business backend

| Module | Status | Notes |
|---|---|---|
| tenants, customers, conversations, messages | ✅ | CRUD + the message intake → Kernel flow |
| products | ✅ | CRUD + real Redis cache-aside (60s TTL, invalidated on write) |
| orders | 🟡 | Read-only stub (`GET /:id`); no create/update — waiting on Tool Engine |
| billing | 🟡 | Records plan/subscription state locally only. **No payment gateway integrated** — `POST /billing/subscription` explicitly does not charge anything |
| channels (WhatsApp real; Facebook still a stub) | ✅ WhatsApp | See "WhatsApp channel (new)" section below for the full breakdown. Facebook Messenger is still unbuilt — only WhatsApp was implemented. |

## Redis — actual usage (not just a connected client)

| Use | Status | Where |
|---|---|---|
| Rate limiting | ✅ | `middleware/rateLimiter.ts`, applied to `POST /messages` (60/tenant/min) |
| Cache-aside | ✅ | `GET /products` (60s TTL) |
| Queues / background jobs | ⬜ | `node-api/src/queues/README.md` is a placeholder only |
| Distributed locks | ⬜ | Not used anywhere |
| Redis in python-api | ⬜ | `redis` is in `requirements.txt` but **never imported/used** by any Python file |

## Python — AI engines

| Engine | File(s) | Status |
|---|---|---|
| Language Engine | `app/language/` | ✅ runs first in Kernel; v2 adds `communication_style`, `is_ambiguous`/`ambiguity_reason`, `entity_spans`, `original_message` — see `docs/LANGUAGE.md`. `entity_spans` is consumed by Intent Engine as an advisory hint; `communication_style`/`is_ambiguous` are logged only, not yet behavior-affecting |
| Intent Engine | `app/intent/` | ✅ classifies on the *normalized* message; now also accepts an optional `entity_hints` param (Language Engine's `entity_spans`, advisory only — Intent Engine still owns all entity typing) |
| State Engine | `app/state/` | ✅ upserts slots from intent entities; `ConversationStatus` enum defined but **never used** |
| Context Engine | `app/context/` | ✅ assembles Memory+RAG+State, but see the RAG caveat below |
| Memory Service (write path) | `app/memory/memory_service.py` | ✅ extraction → confidence gate (0.55) → insert |
| Memory Search (read path) | `app/memory/memory_search.py` | 🟡 `get_all()` used by Context Engine; `semantic_search()` (embedding-based) **defined but never called anywhere** |
| RAG (hybrid search + rerank) | `app/rag/` | 🟡 embedding provider now implemented (OpenAI, requires `OPENAI_API_KEY`); **no knowledge ingestion pipeline exists**, so `knowledge_chunks` is empty and RAG returns zero results even though it no longer errors — see below |
| Kernel | `app/kernel/kernel.py` | ✅ full lifecycle with layered error handling — see `docs/KERNEL.md` |
| Core Agent | `app/agent/` | ✅ thin wrapper around Kernel; now loads per-tenant `AgentConfig` from `agent_configs` each request (see `docs/AGENT.md`) — loaded and logged, not yet consumed by Kernel logic |
| Tool Engine | — | ⬜ not started. Kernel has a marked insertion point for `CREATE_ORDER`/`ORDER_STATUS` that currently just logs and falls through to a plain LLM answer |
| Decision Engine | — | ⬜ not started |
| Negotiation Engine | — | ⬜ not started (intentionally deferred per the original spec) |
| Multimodal (vision/audio) | — | ⬜ not started |

### ✅ Fixed: RAG failure no longer discards Memory

Previously, `context_engine.assemble()` fetched `memories` and then
called `hybrid_search.search()` in the same function body; when RAG
raised (embedding provider unimplemented), the whole function raised
before returning, discarding the already-fetched memories too.

**Fixed:** RAG search now has its own try/except inside `assemble()`.
A RAG failure now only empties `knowledge_chunks` — `memories` are
fetched and returned independently regardless of RAG's outcome.

### RAG now runs without erroring, but has no knowledge to retrieve yet

`embedding_service.py` now calls OpenAI's `text-embedding-3-small`
(1536 dims, matching the schema) instead of raising
`NotImplementedError` — set `OPENAI_API_KEY` in `python-api/.env` to
activate it. However **no ingestion pipeline exists**: nothing has ever
inserted a row into `knowledge_documents`/`knowledge_chunks`, so even
with a working embedding provider, hybrid search will run cleanly and
simply return zero candidates. Building the ingestion pipeline is
`docs/ROADMAP.md` item 2.

## Observability (Phase 1 — see `docs/OBSERVABILITY.md` for full detail)

| Item | Status | Notes |
|---|---|---|
| Request tracing (`x-request-id`) across both services | ✅ | `middleware/requestId.ts` (Node) → header/body → `AgentRequest.requestId` (Python), logged on both sides |
| Request metrics (count/duration/status/error count) | ✅ | `node-api/src/utils/metrics.ts` + `middleware/metrics.ts`; `python-api/app/core/metrics.py` + `request_middleware.py`. In-memory counters, exposed at `GET /metrics` on both services |
| AI/Kernel per-step latency (language/intent/context/memory/RAG/LLM/total) | ✅ measured, ✅ reaches the trace | `kernel.py` + `context_engine.py` time every step; `agent_run_traces` (via migration `007_observability.sql`) stores all of them — see "Agent Run Trace" below |
| Infrastructure metrics (CPU/RAM/uptime) | ✅ foundation | `getInfraSnapshot()` on both services, stdlib/built-ins only, no new dependency. CPU figure is **cumulative**, not instantaneous % — see `docs/OBSERVABILITY.md` |
| Database health check | ✅ | `checkDatabaseHealth()`/`check_database_health()` — cheap `SELECT 1` + pool stats, used only by `/readiness` |
| Redis health check | ✅ node-api, N/A python-api | node-api genuinely depends on Redis; python-api's `requirements.txt` includes `redis` but nothing imports it, so its `/readiness` doesn't fabricate a check for a dependency it doesn't have |
| `GET /health` vs `GET /readiness`, clearly separated | ✅ both services | health = liveness (no dependency calls); readiness = can-serve-a-request (checks DB, +Redis on node-api). Different auth rules per service — see `docs/OBSERVABILITY.md` section 6 |
| Structured JSON logging (Node) | 🟡 | Now used in `ai.client.ts`, `errorHandler.ts`, `middleware/metrics.ts`. Still not applied to every single call site in the codebase |
| Structured logging (Python) | ✅ format, 🟡 unified with Node | `core/logging.py`'s `JsonFormatter` now wraps **every** existing `logger.*()` call as JSON automatically — no call site needed to change. Still snake_case vs Node's camelCase, not normalized across services |
| Error classification (8 categories: API/DB/Redis/LLM/RAG/Memory/Timeout/Validation) | ✅ | `python-api/app/core/error_types.py`, `node-api/src/utils/errorCategory.ts` — same string values, hand-kept in sync. Wired into `kernel.py`'s fallback path and `errorHandler.ts` |
| `agent_run_id` / full request tracing fields | ✅ | Every `agent_run_traces` row carries `agent_run_id` + `request_id` + `tenant_id` + `customer_id` + `conversation_id` + `message_id` together — see "Agent Run Trace" below |
| Docker healthcheck for `python-api`/`node-api` | ✅ | `docker-compose.yml` — polls each service's own `/health`. `postgres`/`redis` already had these; the app services didn't until this phase |

### Agent Run Trace — now carries per-step latency + error category

`agent_run_traces` (unchanged table, extended by `007_observability.sql`)
gained `language_latency_ms`, `intent_latency_ms`, `context_latency_ms`,
`memory_latency_ms`, `rag_latency_ms`, `llm_latency_ms`, `error_category`.
**A real bug was found and fixed while wiring this up**: the Kernel
computed all six latencies and the error category, but
`AgentRunTrace`/`record_trace()` (`python-api/app/trace/`) were never
updated to actually include them in the `INSERT` — the data was
silently discarded before this phase. Fixed, with a regression test
(`test_record_trace_includes_per_step_latency_and_error_category`).

## Error handling

| Boundary | Status |
|---|---|
| Node→Python call fails/times out | ✅ returns a safe bilingual fallback reply, never a raw 500 to the customer (`ai.client.ts`) |
| RAG/Context assembly fails | ✅ isolated inside `assemble()` itself now — Memory fetch is independent and unaffected by a RAG failure (fixed; previously the whole function raised and discarded memories too) |
| Memory extraction fails | ✅ isolated try/except, never blocks the already-generated reply |
| Language/Intent engine fails | 🟡 **not individually isolated** — a failure here falls through to the Kernel's outer try/except, which returns the *generic* fallback reply for the whole turn (not a partial degradation) |
| Node route errors | ✅ centralized `errorHandler.ts` → `AppError` → clean JSON; unknown errors → generic 500, no internals leaked |

## WhatsApp channel (new)

**Status: ✅ implemented, unit-tested where testable without a live Meta account.**

Files: `node-api/src/modules/channels/{whatsapp.signature,whatsapp.parser,
whatsapp.client,whatsapp.webhook,channel_credentials.repository,channels.routes}.ts`,
`db/init/006_channel_credentials.sql`, `middleware/rawBody.ts`.

Flow: `POST /webhooks/whatsapp` (Meta calls this directly) → verify
`X-Hub-Signature-256` against the raw body (`whatsapp.signature.ts`) →
parse the payload (`whatsapp.parser.ts`) → de-duplicate via Redis
(Meta retries webhooks) → look up which tenant owns the receiving
phone number (`channel_credentials`) → resolve/create the customer
(`identity_resolution.service.ts`, deterministic only) →
`processIncomingMessage()` (the same function `POST /messages` uses —
no second Kernel-calling implementation) → send the reply back via
`whatsapp.client.ts`.

A tenant connects their own WhatsApp number via
`POST /channels/whatsapp/credentials` (dashboard, JWT-authed) — stores
`phone_number_id` + `access_token` in `channel_credentials`.

**What's tested:** signature verification (7 tests: valid, wrong
secret, tampered body, missing header, malformed header, empty
secret fails closed, malformed hex doesn't throw) and payload parsing
(7 tests: normal message, status updates ignored, non-text types
skipped-and-counted, multiple entries, malformed payloads, missing
required fields, missing contact profile) — all pure functions, all
passing. Run: compile + `node --test` (see the test files' headers;
same pattern as `identity_resolution.test.ts`).

**What's NOT tested (and can't be, without a live Meta account/network
access in this environment):** the actual `axios` call to Meta's Graph
API in `whatsapp.client.ts`, and the end-to-end webhook handler
(`whatsapp.webhook.ts`'s `handleIncomingWhatsAppMessage`) against a
real Postgres/Redis. The code is written and reviewed but only the
pure-logic pieces have automated proof of correctness.

**Known limitations, disclosed rather than hidden:**
- `channel_credentials.access_token` is stored as **plaintext**. Fine
  for local/MVP use; before real production use this should move to
  an encrypted column or a secrets manager.
- **No message queue** — the webhook processes messages synchronously
  within the request. Fine at low volume; a burst of messages or a
  slow Kernel response risks Meta's webhook timeout. `node-api/src/queues/README.md`
  is where a retry/queue would plug in later.
- **Facebook Messenger is still unbuilt** — only WhatsApp was done.
  The same pattern (signature verify → parse → dedupe → resolve →
  `processIncomingMessage` → reply) should carry over directly.
- Redis dedupe uses a 24-hour TTL per WhatsApp message id — reasonable
  for Meta's actual retry window, not configurable per-tenant yet.

## Testing

| Item | Status | Notes |
|---|---|---|
| `node-api/src/utils/errorCategory.test.ts` | ✅ | 11 tests, all passing. Timeout/DB/Redis/validation detection, priority ordering (timeout beats a DB-sounding message), never throws on a non-Error value |
| `node-api/src/utils/metrics.test.ts` | ✅ | 6 tests, all passing. Request/error counting, per-status tracking, infra snapshot sanity (non-negative, never throws) |
| `python-api/tests/test_observability.py` | ✅ | 18 tests, all passing — mirrors the two Node test files above for parity: `classify_exception()` (11 tests: timeout priority, anthropic/openai→llm_error, sqlalchemy/asyncpg→database_error, redis→redis_error, ValueError/TypeError→validation_error, never raises) + `metrics.py`'s counters (7 tests). Needs no test stubs — `error_types.py`/`metrics.py` have zero external dependencies |
| `python-api/tests/test_agent_foundation.py`'s trace tests | ✅ | Now includes a regression test for the per-step-latency bug fixed in this phase (see "Agent Run Trace" above) |
| `python-api/tests/test_language_engine.py` | ✅ | First real test file in the repo (previously `tests/` was empty on both services). 14 tests, all passing. Exercises the actual `language_engine.py`/`intent_engine.py` production code with only `ai_service.complete_json` (the LLM network boundary) mocked — covers Bangla, Banglish, mixed Bangla-English, short contextual follow-ups, ambiguous input, original-message preservation, and defensive parsing of malformed LLM output. Run: `docker compose exec python-api python -m unittest tests.test_language_engine -v` |
| `python-api/tests/test_agent_foundation.py` | ✅ | 12 tests, all passing. Covers Capability Registry (register/get/duplicate-rejection/starts-empty), Goal/Outcome (pure-function determinism), Tenant Brain (composes `agent_configs` + `business_rules`, defensive fallback on either query failing), and Agent Run Trace (insert+commit shape, isolated-failure-never-raises). Uses a fake in-memory DB session, not a real Postgres connection. Run: `docker compose exec python-api python -m unittest tests.test_agent_foundation -v` |
| `node-api/src/modules/customers/identity_resolution.test.ts` | ✅ | 8 tests, all passing (Node's built-in `node:test`). Priority order, tenant isolation, no-accidental-cross-matching. |
| `node-api/src/modules/channels/whatsapp.signature.test.ts` | ✅ | 7 tests, all passing. Valid/invalid/tampered/missing signatures, fails closed on an empty secret. |
| `node-api/src/modules/channels/whatsapp.parser.test.ts` | ✅ | 7 tests, all passing. Normal messages, status updates ignored, non-text types skipped, malformed payloads handled without throwing. |
| `node-api/src/modules/customers/identity_resolution.test.ts` | ✅ | 8 tests, all passing (Node's built-in `node:test` runner). Covers deterministic match priority (channel > phone > email > none), tenant isolation, and that `resolveOrLinkCustomer` only links when nothing matched. Run: `npx tsx --test src/modules/customers/identity_resolution.test.ts` (or compile with `tsc` then `node --test`) |
| Everything else | ⬜ | No other test files exist yet, on either service |

## Billing

🟡 Plan/subscription state is recorded locally (`subscriptions` table).
**No payment provider is integrated** — this was a deliberate choice
(no gateway keys/network available to do it honestly). See the
extension point documented in `billing.routes.ts`.

## Frontend

⬜ **The real dashboard is still not started.** `web/app/login/`,
`web/app/dashboard/`, `web/lib/` exist as empty directories only — zero
files. Planned as a separate Next.js app, sibling to `node-api`/
`python-api` in the same repo (see `docs/ROADMAP.md`).

🟡 **One interim static page exists**, served directly by `node-api`
(no separate server, no build step): `node-api/public/connect-whatsapp.html`
— plain HTML/CSS/vanilla JS, reachable at `GET /connect-whatsapp.html`.
Lets a tenant log in (`POST /auth/login`) and connect/rotate their
WhatsApp `phoneNumberId`/`accessToken` (`GET`/`POST
/channels/whatsapp/credentials`) without using `curl` directly. This is
a stopgap for one specific task, not a preview of the real dashboard —
it doesn't share code with the eventual Next.js app and isn't meant to
be extended into one.
