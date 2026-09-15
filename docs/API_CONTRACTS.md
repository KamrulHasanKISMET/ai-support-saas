# docs/API_CONTRACTS.md

Every endpoint that actually exists right now, verified against the
route files directly (not against intent/comments).

## node-api (port 4000) — the only service customers/dashboard/channels talk to

### Public (no auth)

| Method & path | Body | Success response | Notes |
|---|---|---|---|
| `GET /health` | — | `{status, service}` | |
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
| `POST /customers` | `{displayName?, phone?, email?}` | 201 customer row | |
| `GET /customers/:id` | — | customer row or 404 | |
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
| `GET /products` | — | array of products | Redis cache-aside, 60s TTL. `X-Cache: HIT`/`MISS` response header. |
| `POST /products` | `{sku?, name, price, stock?}` | 201 product row | Invalidates the product-list cache. |
| `GET /orders/:id` | — | order row or `null` | Read-only — no create/update route exists. |
| `GET /billing/subscription` | — | subscription row, or `{tenant_plan:'trial', status:'active', provider:null}` if none | |
| `POST /billing/subscription` | `{plan}` | `{status:'recorded', plan, note}` | `plan` ∈ `trial\|starter\|growth\|scale`. **Does not charge anything** — no payment gateway is connected. |

## python-api (port 8000) — the AI service

Every route except `/health` requires an `x-internal-secret` header
matching `INTERNAL_SERVICE_SECRET` (set once in the root `.env`,
passed by `docker-compose.yml` to both `node-api` and `python-api`).
Enforced by the `require_internal_secret` FastAPI dependency
(`app/core/security.py`), applied at the router level. **Fails closed**
if the secret is unset on the server side — every request is rejected,
not silently allowed through. `node-api`'s `ai.client.ts` sends this
header on every call automatically; nothing else should be able to
reach this service in production (don't expose port 8000 publicly).

| Method & path | Body/Query | Success response | Notes |
|---|---|---|---|
| `GET /health` | — | `{status, service}` | No auth — used for container health checks |
| `POST /ai/kernel/run` | `AgentRequest`: `{tenantId, customerId, conversationId, messageId, message, channel?, metadata?, requestId?}` | `AgentResponse`: `{reply, intent?, confidence?, state, toolsCalled, metadata}` | Requires `x-internal-secret`. The only route `node-api` calls. `requestId` also accepted via `x-request-id` header as a fallback if not in the body. |
| `GET /rag/search?tenantId=&q=` | — | `{query, results}` | Requires `x-internal-secret`. Debug only. Runs without erroring now, but returns empty results — see `docs/RAG.md` (no knowledge has ever been ingested). |
| `GET /memory/?tenantId=&customerId=` | — | `{tenantId, customerId, memories}` | Requires `x-internal-secret`. Debug only. |

## Request tracing

Both services propagate `x-request-id`: `node-api`'s `requestId`
middleware assigns/reuses one per request, forwards it to `python-api`
(header + `AgentRequest.requestId`), and both sides log it on every
line related to that request — grep logs by request id to trace one
customer interaction across both services.
