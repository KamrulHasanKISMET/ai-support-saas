# docs/API_CONTRACTS.md

Every endpoint that actually exists right now, verified against the
route files directly (not against intent/comments).

## node-api (port 4000) — the only service customers/dashboard/channels talk to

### Public (no auth)

| Method & path | Body | Success response | Notes |
|---|---|---|---|
| `GET /health` | — | `{status, service, process}` | Liveness only, no dependency calls. |
| `GET /readiness` | — | `{status, service, dependencies:{database, redis}, process}` (503 if not ready) | Checks Postgres + Redis. See `src/modules/health/health.routes.ts`. |
| `GET /metrics` | — | Prometheus text exposition format | Production Reliability workstream (`docs/RELIABILITY.md`). Previously required a JWT; now unauthenticated so Prometheus can scrape it over the internal `docker-compose` network — don't expose port 4000's `/metrics` path publicly. |
| `GET /connect-whatsapp.html` | — | static HTML page | Minimal, dependency-free settings page (login + connect/rotate WhatsApp credentials) served directly by `express.static` — not a JSON endpoint. See `PROJECT_STATUS.md`'s Frontend section. |
| `POST /auth/signup` | `{businessName, slug, email, password}` | 201 `{tenant:{id,name,slug,apiKey}, user:{id,email,role}, token}` | Creates tenant + owner user + api_key in one DB transaction. `password` ≥ 8 chars. 409 on duplicate slug/email. |
| `POST /auth/login` | `{tenantSlug, email, password}` | `{user:{id,email,role}, token}` | Email is only unique **per tenant**, hence `tenantSlug` is required. 401 on bad credentials. |
| `POST /tenants` | `{name, slug}` | 201 full tenant row (incl. `api_key`, shown once) | Platform-admin utility — creates a tenant with **no user**, so there's no way to `/auth/login` into it afterward. Prefer `/auth/signup` for a usable account. |
| `GET /tenants/:slug` | — | tenant row **minus `api_key`** | Fixed during this audit — previously leaked `api_key` to any caller who knew/guessed a slug. |

### Channel-facing (require `x-api-key` header — `requireApiKey`)

The key is a tenant's `api_key` (from signup/creation response),
looked up against `tenants.api_key`. 401 if missing/invalid/inactive.

| Method & path | Body | Success response | Notes |
|---|---|---|---|
| `POST /customers` | `{displayName?, phone?, email?}` | 201 customer row | Also reachable with a dashboard JWT — see the note under "Dashboard-facing" below. |
| `GET /customers/:id` | — | customer row or 404 | Also reachable with a dashboard JWT — see the note below. |
| `POST /messages` | `{customerId, channel, content}` | 201 `{conversationId, customerMessage, aiMessage, intent, confidence, language}` | **Rate-limited: 60 requests/tenant/60s** (`X-RateLimit-*` response headers). Resolves/creates an open conversation, stores the customer message, calls the Kernel, stores + returns the AI reply. Never throws a raw 500 to the caller — on Kernel/network failure returns a safe fallback reply (see `ai.client.ts`). Now a thin wrapper around `processIncomingMessage()` (`messages.service.ts`) — the WhatsApp webhook below calls the same function. |

### Inbound webhooks (provider signature is the auth — NOT `x-api-key`/JWT)

Called directly by the channel provider (Meta), not by our own channel
adapters or the dashboard. Meta cannot send our custom headers, so
these routes sit outside `requireApiKey`/`requireAuth` entirely — the
provider's own request signature is what authenticates the call.

| Method & path | Body/Query | Success response | Notes |
|---|---|---|---|
| `GET /webhooks/whatsapp` | query: `hub.mode`, `hub.verify_token`, `hub.challenge` | 200 echoes `hub.challenge`, or 403 | Meta's one-time handshake when you configure the webhook URL in the Meta App dashboard. Compares `hub.verify_token` against `WHATSAPP_VERIFY_TOKEN`. |
| `POST /webhooks/whatsapp` | Meta's webhook payload (see `whatsapp.parser.ts`) | Always `200` immediately (Meta expects a fast ack) | Verifies `X-Hub-Signature-256` against the raw body (`whatsapp.signature.ts`) — **401 and nothing else happens if invalid**. On a valid signature: de-duplicates by WhatsApp message id (Redis, 24h TTL), looks up the owning tenant via `channel_credentials`, resolves/creates the customer (`identity_resolution.service.ts`, deterministic only), calls `processIncomingMessage()`, sends the reply back via the WhatsApp Cloud API. All of this happens **after** the 200 response — failures are logged, never surfaced as an HTTP error (there's no one left listening). |

### Dashboard-facing (require `Authorization: Bearer <jwt>` — `requireAuth`)

JWT issued by `/auth/signup` or `/auth/login`, 7-day expiry.
`tenantId`/`userId`/`role` come from verified token claims, never from
client-supplied headers.

| Method & path | Body | Success response | Notes |
|---|---|---|---|
| `GET /channels` | — | `{status:"ok", channels:["whatsapp"], todo:["facebook"]}` | Lists which channels this deployment supports connecting. |
| `GET /channels/whatsapp/credentials` | — | `{connected:false}` or `{connected:true, externalAccountId, isActive}` | Never returns `access_token` back once stored. |
| `POST /channels/whatsapp/credentials` | `{phoneNumberId, accessToken}` | 201 `{connected:true, externalAccountId}` | Connects (or rotates the token for) this tenant's WhatsApp Business number. Does not create/verify anything with Meta — just stores what you already set up in your own Meta App/Business account. 409 if that `phoneNumberId` is already connected to a **different** tenant. |
| `GET /conversations/:id` | — | conversation row or 404 | |
| `GET /conversations/:id/messages` | — | array of messages (asc by time, limit 50) | Moved here from the old `/messages` router during this audit — a dashboard JWT holder never has the tenant's `x-api-key`, so this couldn't have lived under `requireApiKey`. |
| `GET /products?categoryId=` | — | array of products | Redis cache-aside, 60s TTL (per-category cache key when filtered). `X-Cache: HIT`/`MISS` response header. |
| `POST /products` | `{sku?, name, price, stock?, categoryId?}` | 201 product row | Invalidates the product-list cache (all category variants for this tenant). |
| `GET /orders/:id` | — | order row or `null` | Read-only — no create/update route exists. |
| `GET /billing/subscription` | — | subscription row, or `{tenant_plan:'trial', status:'active', provider:null}` if none | |
| `POST /billing/subscription` | `{plan}` | `{status:'recorded', plan, note}` | `plan` ∈ `trial\|starter\|growth\|scale`. **Does not charge anything** — no payment gateway is connected. |
| `GET /customers?search=&limit=&offset=` | — | `{customers, total, limit, offset}` | List/search (matches `display_name`/`phone`/`email`) with pagination (`limit` ≤ 200, default 50). CUSTOMER-KNOWLEDGE-RAG-API-001 phase 1. |
| `PATCH /customers/:id` | `{displayName?, phone?, email?, metadata?}` | updated customer row or 404 | Partial update — omitted fields are left unchanged. |
| `DELETE /customers/:id` | — | 204 or 404 | Relies on existing `ON DELETE` FK behavior for dependent rows (conversations/orders/channel identities) — unchanged by this task. |

`/customers` is the one path in this file reachable by **either**
`x-api-key` **or** a dashboard JWT (`requireApiKeyOrAuth` — see
`middleware/auth.ts`): the website widget creates/reads its own
visitor's record with an api key, staff list/search/update/delete
theirs with a JWT. Every other router in this file accepts exactly
one credential type.

### Product Categories (`requireAuth`) — CUSTOMER-KNOWLEDGE-RAG-API-001 phase 2

| Method & path | Body | Success response | Notes |
|---|---|---|---|
| `GET /product-categories` | — | `{categories}` | Flat list (includes `parent_id` for hierarchy; the client builds the tree). |
| `POST /product-categories` | `{name, slug?, description?, parentId?}` | 201 category row | `slug` is derived from `name` if omitted, always slugified. 409 on duplicate `(tenant_id, slug)`. 400 if `parentId` doesn't exist for this tenant. |
| `GET /product-categories/:id` | — | category row or 404 | |
| `PATCH /product-categories/:id` | `{name?, slug?, description?, parentId?}` | updated row or 404 | 400 if `parentId` would make the category its own parent. 409 on duplicate slug. |
| `DELETE /product-categories/:id` | — | 204 or 404 | Products/child categories pointing at this one are set to `NULL` (uncategorized), never cascade-deleted — see `db/init/009_knowledge_customer_category.sql`. |

### Knowledge Base (`requireAuth`) — CUSTOMER-KNOWLEDGE-RAG-API-001 phases 3-11

| Method & path | Body | Success response | Notes |
|---|---|---|---|
| `POST /knowledge/documents` | `multipart/form-data`: `file` (pdf/docx/txt/csv, ≤`MAX_UPLOAD_SIZE_BYTES`, 20MB default), `title?`, `category?`, `language?` | 201 document row (`status: "UPLOADED"`) | `category` must be one of the fixed set (General/Products/Pricing/Delivery/Return/Refund/Warranty/FAQ/Company Information/Custom) or omitted. Extension + declared Content-Type + magic-byte signature must all agree (`utils/fileValidation.ts`) — 400 otherwise. File is stored via `utils/storage.ts` (local disk under a named Docker volume by default). |
| `GET /knowledge/documents?status=&category=&limit=&offset=` | — | `{documents, total, limit, offset}` | |
| `GET /knowledge/documents/:id` | — | full document row (incl. `raw_content`, capped at 100k chars) or 404 | |
| `GET /knowledge/documents/:id/status` | — | `{id, status, chunks, category, error?, processed_at}` | Safe projection — never `raw_content`, `storage_path`, or embeddings. |
| `DELETE /knowledge/documents/:id` | — | 204 or 404 | Deletes the stored file (best-effort) and the DB row; `knowledge_chunks` cascade-delete via FK. |
| `POST /knowledge/documents/:id/process` | — | updated document row (`status: "READY"` or `"FAILED"`, with `chunk_count`/`error_message`) | **Synchronous** — the HTTP response waits for the full extract→chunk→embed→store pipeline in python-api to finish (no job queue exists yet, see `node-api/src/queues/README.md`); can take a while for large documents. Idempotent: re-running replaces this document's chunks rather than duplicating them. |
| `GET /knowledge/faq?category=` | — | `{faqs}` | |
| `POST /knowledge/faq` | `{question, answer, category?}` | 201 FAQ row | Embedded into RAG search as a single chunk (best-effort — a failed embed still saves the FAQ; `knowledge_document_id` stays `null` until it succeeds). |
| `PATCH /knowledge/faq/:id` | `{question?, answer?, category?}` | updated FAQ row or 404 | Re-embeds with the merged question/answer/category. |
| `DELETE /knowledge/faq/:id` | — | 204 or 404 | |

RAG search itself (`GET /rag/search` below, and the Kernel's own
retrieval) was already tenant-scoped before this task — every
`knowledge_chunks` query filters by `tenant_id` at the SQL level (see
`python-api/app/rag/search.py`); nothing here changes that.

## python-api (port 8000) — the AI service

Every route except `/health` and `/metrics` requires an
`x-internal-secret` header matching `INTERNAL_SERVICE_SECRET` (set
once in the root `.env`, passed by `docker-compose.yml` to both
`node-api` and `python-api`). Enforced by the `require_internal_secret`
FastAPI dependency (`app/core/security.py`), applied at the router
level. **Fails closed** if the secret is unset on the server side —
every request is rejected, not silently allowed through. `node-api`'s
`ai.client.ts` sends this header on every call automatically; nothing
else should be able to reach this service in production (don't expose
port 8000 publicly).

`/metrics` (Production Reliability workstream, see
`docs/RELIABILITY.md`) is the one exception besides `/health`:
Prometheus has no way to present `x-internal-secret` on a scrape, so
it is intentionally unauthenticated and relies on Docker network
isolation instead (`monitoring/prometheus.yml` scrapes it over the
internal `docker-compose` network only) — do not expose port 8000
publicly.

| Method & path | Body/Query | Success response | Notes |
|---|---|---|---|
| `GET /health` | — | `{status, service, process}` | No auth — used for container health checks. Liveness only, no dependency calls. |
| `GET /readiness` | — | `{status, service, dependencies:{database}, process}` (503 if not ready) | Requires `x-internal-secret`. Checks Postgres. See `app/api/routes/readiness.py`. |
| `GET /metrics` | — | Prometheus text exposition format | No auth (see note above). HTTP + DB pool metrics, scraped by `monitoring/prometheus.yml`. |
| `POST /ai/kernel/run` | `AgentRequest`: `{tenantId, customerId, conversationId, messageId, message, channel?, metadata?, requestId?}` | `AgentResponse`: `{reply, intent?, confidence?, state, toolsCalled, metadata}` | Requires `x-internal-secret`. The only route `node-api` calls. `requestId` also accepted via `x-request-id` header as a fallback if not in the body. |
| `GET /rag/search?tenantId=&q=` | — | `{query, results}` | Requires `x-internal-secret`. Debug only. Runs without erroring now, but returns empty results — see `docs/RAG.md` (no knowledge has ever been ingested). |
| `POST /knowledge/ingest` | `multipart/form-data`: `tenantId`, `documentId`, `file`, `category?`, `language?` | `{status, chunkCount, errorMessage?}` | Requires `x-internal-secret`. Called only by node-api (`knowledge.client.ts`) after `POST /knowledge/documents/:id/process`. Runs extract→chunk→embed→store synchronously; writes `knowledge_documents.status` progressively (`EXTRACTED`→`CHUNKED`→`EMBEDDING`→`READY`/`FAILED`) so `GET /knowledge/documents/:id/status` on node-api reflects real progress. Idempotent — deletes and reinserts this document's `knowledge_chunks` rather than appending. |
| `POST /knowledge/faq/ingest` | `multipart/form-data`: `tenantId`, `faqId`, `question`, `answer`, `category?` | `{knowledgeDocumentId}` | Requires `x-internal-secret`. Called by node-api after `POST`/`PATCH /knowledge/faq`. Embeds the Q&A pair as one chunk via a per-FAQ synthetic `knowledge_documents` row (found-or-created by a stable `"FAQ #<id>"` title), so FAQs are retrievable by the same RAG search path as uploaded documents. |
| `GET /memory/?tenantId=&customerId=` | — | `{tenantId, customerId, memories}` | Requires `x-internal-secret`. Debug only. |

## Request tracing

Both services propagate `x-request-id`: `node-api`'s `requestId`
middleware assigns/reuses one per request, forwards it to `python-api`
(header + `AgentRequest.requestId`), and both sides log it on every
line related to that request — grep logs by request id to trace one
customer interaction across both services.
