# docs/ROADMAP.md

Ordered by what's most urgent given the current, verified state of the
repo (see `PROJECT_STATUS.md`) — not a re-statement of the original
spec's roadmap, which has already been substantially completed.

## 1. Security gaps (do these before any real deployment)

- [x] Add service-to-service auth between `node-api` and `python-api`
  — `x-internal-secret` header, fails closed if unset. See
  `docs/API_CONTRACTS.md`.
- [ ] Consider also network-isolating port 8000 (don't publish it to
  the host) as defense in depth — the shared secret helps, but the
  port is still reachable today per `docker-compose.yml`.
- [x] ~~Add auth to the Python debug routes~~ — done as part of the
  service-to-service fix above; `/rag/search` and `/memory/` now
  require the same `x-internal-secret`.
- [ ] Role-based permission checks on dashboard routes — `role` exists
  on the JWT claim but nothing checks it yet.

## 2. Fix RAG (unblocks Memory too)

- [x] Implement `EmbeddingService.embed()` — now calls OpenAI's
  `text-embedding-3-small`, set `OPENAI_API_KEY` to activate.
- [ ] Build a knowledge ingestion pipeline (upload → clean → chunk →
  embed → store) — `knowledge_documents`/`knowledge_chunks` tables
  exist but **still nothing populates them**. RAG runs cleanly now but
  has zero rows to retrieve until this is built.
- [x] ~~Independently fix the Memory-loss side effect~~ — done;
  `context_engine.assemble()` now isolates the RAG call so a failure
  there can't discard already-fetched memories. See `docs/MEMORY.md`.

## 3. Tool Engine + Decision Engine

- [ ] Give the Kernel real tools (`check_product_stock`,
  `check_order_status`, `create_order`, ...) behind a
  Propose → Permission Check → Business Rule → Validate → Execute
  pipeline. The insertion point is already marked in `kernel.py` (the
  `if intent_result.intent in (CREATE_ORDER, ORDER_STATUS)` block).
- [ ] Wire up `business_rules` (table exists, unused) so Tool execution
  can be constrained by tenant-configured rules (discount limits, etc).
- [ ] Read `agent_configs` from Postgres into `AgentConfig` instead of
  the current hardcoded default — this is what makes verticals
  (real estate, construction, etc.) actually configurable per-tenant
  instead of just conceptually possible.
  **Loading is done** (`CoreAgent.run()` calls `load_agent_config()`
  every request — see `docs/AGENT.md`); what's still missing is any
  Kernel logic that actually branches on `vertical`/`tools_enabled`.

## 4. Frontend

`web/` is an empty scaffold. Build (as a separate Next.js app, sibling
to `node-api`/`python-api`, same repo — see `AI_CONTEXT.md`):
- [ ] Login/signup pages calling `/auth/login` and `/auth/signup`
- [ ] Conversation list + detail view (`GET /conversations/:id/messages`)
- [ ] Product management (`GET`/`POST /products`)
- [ ] Billing page (`GET`/`POST /billing/subscription`)

## 5. Observability

**Phase 1 (request/AI metrics, health/readiness, error classification,
structured logging, trace latency columns) is done — see
`docs/OBSERVABILITY.md` for the full breakdown.** What's left:

- [x] ~~Apply structured JSON logging consistently~~ — Python's
  `JsonFormatter` now wraps every existing `logger.*()` call
  automatically; Node's `errorHandler.ts` and the new `metrics.ts`
  middleware use `utils/logger.ts` now. Not every single Node call
  site does yet (see `docs/OBSERVABILITY.md` section 7).
- [ ] Unify logging **field naming** between Node (camelCase) and
  Python (snake_case) — both are JSON now, but a log aggregator still
  needs to normalize key names across services rather than query one
  shared key. Deliberately out of scope for Phase 1 (would mean
  renaming fields across many existing call sites).
- [ ] A read/query API for `agent_run_traces` — still write-only
  (deliberate; "not a complete analytics platform" applies to Phase 1
  too). Query it directly with SQL for now.
- [ ] Cost tracking (`agent_run_traces.cost_usd` stays `NULL`) — needs
  a per-model token-pricing table that doesn't exist yet.
- [ ] Phase 2+: Prometheus/OpenMetrics format, Grafana, alerting, load
  testing, Kubernetes, autoscaling — explicitly deferred, not part of
  Phase 1's foundation-only scope.

## 6. Channels

- [x] ~~Real WhatsApp webhook handler~~ — done. Signature verification,
  payload parsing, Redis dedupe, deterministic identity resolution, and
  reply-sending are all implemented and unit-tested where testable
  without a live Meta account. See `PROJECT_STATUS.md`'s "WhatsApp
  channel" section for exactly what is/isn't covered by automated
  tests (the real Graph API call and a live Postgres/Redis
  integration test are the two things still unverified end-to-end).
- [ ] Facebook Messenger webhook handler — still unbuilt. The same
  pattern WhatsApp used should carry over directly: signature verify
  → parse payload → Redis dedupe → resolve/create customer via
  `identity_resolution.service.ts` → `processIncomingMessage()` → send
  reply via Facebook's Graph API. Needs its own `channel_credentials`
  row type (`channel = 'facebook'`) and its own signature-verification
  scheme (Facebook Messenger's is HMAC-SHA1, not WhatsApp's SHA256 —
  don't assume `whatsapp.signature.ts` is directly reusable, check
  Meta's current docs for Messenger specifically).
- [ ] A live end-to-end test against real Postgres + Redis (not just
  the pure-logic unit tests that exist today) — see
  `PROJECT_STATUS.md`'s WhatsApp section for what's not yet covered.
- [ ] Move `channel_credentials.access_token` off plaintext storage
  (encrypted column or a secrets manager) before real production use —
  flagged explicitly in `db/init/006_channel_credentials.sql`'s comment,
  not yet done.

## 7. Billing

- [ ] Integrate a real payment provider (Stripe/SSLCommerz/bKash).
  The extension point and the exact steps needed are documented as
  comments in `billing.routes.ts`.

## Explicitly deferred (matches the original spec's own guidance — don't build early)

- Negotiation Engine
- Multimodal (vision/audio) input
- Multi-agent orchestration
- Extracting Core Agent into its own microservice
