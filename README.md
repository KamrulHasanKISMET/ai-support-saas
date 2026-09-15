# Multi-Tenant AI Support SaaS

Reusable **AI Agent Kernel** powering a multi-tenant customer-support product.
See the full architecture spec this scaffold implements for the complete
vision — this README covers only "how to run what's here."

## Stack

```
Node.js (Express, TS)  -> business backend: tenants, customers, conversations,
                           messages, channels, products, orders
Python (FastAPI)       -> AI service: Kernel, Intent, State, Context, RAG, Memory
PostgreSQL + pgvector  -> single source of truth
Redis                  -> cache / queues / temp state (not persistent)
Docker Compose         -> local orchestration
```

## Quick start

```bash
cp .env.example .env
cp node-api/.env.example node-api/.env
cp python-api/.env.example python-api/.env
# fill in ANTHROPIC_API_KEY in python-api/.env

docker compose up --build
```

- Node API      → http://localhost:4000/health
- Python AI API → http://localhost:8000/health
- Postgres      → localhost:5432
- Redis         → localhost:6379

Database tables are created automatically on first boot from
`db/init/*.sql` (mounted into Postgres's `docker-entrypoint-initdb.d`).

## Try the end-to-end flow

```bash
# 1. Create a tenant
curl -X POST http://localhost:4000/tenants \
  -H "Content-Type: application/json" \
  -d '{"name": "Test Shop", "slug": "test-shop"}'
# -> note the returned id, use it as x-tenant-id below

# 2. Create a customer
curl -X POST http://localhost:4000/customers \
  -H "Content-Type: application/json" -H "x-tenant-id: 1" \
  -d '{"displayName": "Kamrul", "phone": "01700000000"}'

# 3. Send a message -> triggers the Kernel (Intent -> State -> Context -> LLM)
curl -X POST http://localhost:4000/messages \
  -H "Content-Type: application/json" -H "x-tenant-id: 1" \
  -d '{"customerId": 1, "channel": "website", "content": "Nike Air Max এর দাম কত?"}'
```

## What's implemented (current position)

Per the development sequence (Hybrid Search → Re-ranking → Memory →
Context Engine → Kernel v1 → Intent Engine → **State Engine v1**):

- [x] Docker Compose + folder structure
- [x] PostgreSQL schema (tenant-isolated core tables + knowledge/RAG/memory/state)
- [x] Node.js business backend (tenants, customers, conversations, messages, channel-identity linking, products/orders stubs)
- [x] Python AI service skeleton (FastAPI, config, DB session, logging)
- [x] Hybrid search (vector + keyword) + re-ranker
- [x] Memory extraction + validated write path
- [x] Context Engine (memory + RAG + state → unified prompt)
- [x] Language Engine (`app/language/`) — detects Bangla/English/Banglish/mixed input, normalizes it for Intent classification + RAG retrieval, and decides reply language, without guessing locale/nationality
- [x] Intent Engine (LLM classification + confidence)
- [x] State Engine (slot tracking across turns)
- [x] Kernel v1 (Understand → State → Decide/clarify → Retrieve → Reason → Respond → Learn)
- [x] Core Agent v1 (`app/agent/`) — thin runtime entry point in front of the Kernel; see "Core Agent layer" below

## Language Engine (new)

`app/language/language_engine.py` runs as the FIRST step inside the
Kernel, before Intent classification. It answers three separate
questions per architecture doc section 14 — language, reply language,
and meaning are NOT the same thing, and none of them should be used to
guess who the customer is:

1. **What did the customer actually write in?** — Bangla, English,
   Banglish (Bangla in Latin letters), a mix in one sentence, informal
   spelling, abbreviations, emoji.
2. **What language should the reply be in?** — inferred only from the
   message itself, never from locale/nationality assumptions.
3. **What does the customer mean?** — a short English paraphrase
   (`normalizedMessage`) that preserves every fact/entity exactly. This
   is used ONLY internally (Intent classification + RAG retrieval query)
   so "Nike Air Max এর দাম কত?", "nike air max price koto?", and "What's
   the price of the Nike Air Max?" all classify and retrieve consistently.

What this does NOT change:
- The **original message** is still what's stored in Postgres, still
  what Memory Extraction reads, and still what's shown to the LLM as
  "CUSTOMER QUESTION" in the final prompt — normalization never
  replaces it, it only assists classification/retrieval.
- The reply-language decision is passed to the LLM as an explicit
  instruction (`Reply in this language: ...`) rather than the vaguer
  old instruction to "match the customer's tone."

## Core Agent layer (new)

`app/agent/core_agent.py` is now the runtime entry point that
`POST /ai/kernel/run` calls into. It does not replace or duplicate the
Kernel, RAG, Memory, Context, Intent, or State engines — it just wraps
`kernel.run(...)` behind a stable `AgentRequest` / `AgentResponse`
contract (`app/schemas/agent.py`) so future capabilities (multiple
verticals, tool-calling, human escalation) can be added by extending
Core Agent/Kernel without changing what Node.js sends or expects back.

```
Node.js  ->  POST /ai/kernel/run  ->  CoreAgent.run()  ->  kernel.run()  ->  (unchanged) Intent/State/Context/RAG/Memory/LLM
```

**Nothing changed for Node.js.** The URL, required JSON fields, and
response shape are identical to before — `AgentRequest`/`AgentResponse`
are supersets of the old `KernelRunRequest`/`KernelRunResponse` (only
new *optional* fields — `channel`, `metadata` — were added). No changes
were needed in `node-api/src/modules/ai/ai.client.ts`.

`app/agent/agent_config.py` holds a placeholder `AgentConfig` — the
future home for per-tenant/vertical knobs (which vertical, whether
tool-calling is enabled) once the `agent_configs` table is actually
read from. It's intentionally empty of logic right now.

**Language metadata now flows all the way to the dashboard.**
`AgentResponse.metadata.language = {detected, reply}` is set by
`CoreAgent` from the Kernel's `detectedLanguage`/`replyLanguage`
fields. `node-api` stores it on the AI message's `metadata` column
(`GET /messages/conversation/:id` shows it per-message) and also
returns it directly in the `POST /messages` response as `language`.

**Bug fix while wiring this up:** `node-api/src/modules/ai/ai.client.ts`
was calling `POST /api/ai/kernel/run`, but the Python service mounts
that route at `/ai/kernel/run` (no `/api` prefix — see `app/main.py`).
Every Node → Python call would have 404'd. Fixed to call the correct
path; no other behavior changed.

## Not yet implemented — next steps in order

1. **Embedding provider wiring** — `python-api/app/ai/embedding_service.py` currently raises `NotImplementedError`. Plug in your provider (OpenAI, Voyage, etc.) and keep the `vector(1536)` column dimension in sync.
2. **Knowledge ingestion pipeline** — document upload → clean → chunk → embed → store (section 10). No route exists for this yet; add `POST /rag/ingest` (or a Node.js upload endpoint that calls it).
3. **Decision Engine** — business-rule-aware routing between "just answer" vs. "propose a tool" vs. "escalate."
4. **Tool Engine** — `check_product_stock`, `check_order_status`, `create_order`, etc., behind the Propose → Permission Check → Business Rule → Validate → Execute pipeline (section 24). The Kernel already has a marked insertion point for this in `app/kernel/kernel.py`.
5. **Business Rules table wiring** — `business_rules` table exists; nothing reads it yet.
6. **Negotiation Engine** — deliberately deferred until Tools + Business Rules exist (section 26).
7. **Multimodal input** (image/video) — section 27.
8. **Observability** — structured request tracing (`request_id`, `tenant_id`, `intent`, `tools_called`, `latency`, `token_usage`) — section 33.
9. **Auth** — `users` table exists; no login/JWT issuance yet in `node-api`.
10. **Channel adapters** — `channels.routes.ts` is a stub; WhatsApp/Facebook webhook handlers still need to be written (they should end by calling `POST /messages`, not duplicate Kernel-calling logic).

## Development rules (keep following these as you extend the code)

- Every tenant-scoped query filters by `tenant_id` — no exceptions.
- The LLM never writes to the database directly; it proposes, application code validates and writes.
- Intent / State / Context / Memory / RAG / Decision / Tool / Action stay in separate modules even though the Kernel orchestrates all of them.
- Don't reach for LangChain or a bigger framework until the manual version of RAG/Memory/Context/Intent/State/Kernel is understood and working.
- Build one layer at a time — see "Not yet implemented" above for the order.
