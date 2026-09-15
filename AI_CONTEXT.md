# AI_CONTEXT.md — Read this first

This file is the entry point for any AI coding session working on this
repository. **The repository is the source of truth** — do not rely on
a master prompt pasted into chat; read the code and the docs below.

## Golden rule

```
INTEGRATE → PRESERVE → EVOLVE → TEST
```

Do not rewrite the Kernel, RAG, Memory, Context, Intent, State,
Language Engine, or Core Agent. Do not rebuild Docker. Add new
capability by extending these, the same way Language Engine and Core
Agent were added on top of an already-working Kernel.

## What this project is

A multi-tenant AI customer-support backend. Two services:

- **`node-api`** (Express/TypeScript) — the business backend: tenants,
  auth, customers, conversations, messages, products, orders, billing.
  Never does AI reasoning itself.
- **`python-api`** (FastAPI) — the AI service: Core Agent → Kernel →
  Language/Intent/State/Context/RAG/Memory engines → LLM (Claude).

Postgres (+pgvector) is the only source of truth. Redis is cache/rate-
limiting/queues only, never durable state.

## Where to look for what

| Question | Read |
|---|---|
| What's actually built vs. planned? | `PROJECT_STATUS.md` (read this second) |
| Overall system shape, services, data flow | `docs/ARCHITECTURE.md` |
| How a message flows through the Kernel | `docs/KERNEL.md` |
| How mixed Bangla/English input is handled | `docs/LANGUAGE.md` |
| How customer facts are extracted/stored | `docs/MEMORY.md` |
| How knowledge retrieval works (and why it currently fails) | `docs/RAG.md` |
| What Core Agent is and isn't | `docs/AGENT.md` |
| Metrics, health checks, structured logging, error categories | `docs/OBSERVABILITY.md` |
| Every HTTP endpoint, its auth requirement, request/response shape | `docs/API_CONTRACTS.md` |
| What to build next, in order | `docs/ROADMAP.md` |

## Four things to know before touching anything

1. **RAG runs without erroring now, but has nothing to retrieve.**
   `embedding_service.py` calls OpenAI (needs `OPENAI_API_KEY`) instead
   of raising `NotImplementedError`. But no ingestion pipeline exists —
   `knowledge_chunks` is empty, so hybrid search returns zero results
   cleanly. Memory is no longer collateral damage of RAG failing (that
   was fixed) — see `docs/RAG.md` / `docs/MEMORY.md`.
2. **There is no frontend.** `web/` is an empty scaffold folder only.
   Everything is API-only right now.
3. **Auth is real on both services now.** `x-api-key` (per-tenant
   secret) protects channel-facing `node-api` routes; JWT protects
   dashboard routes; `x-internal-secret` (shared, set once in root
   `.env`) protects every `python-api` route except `/health`. See
   `docs/API_CONTRACTS.md`. No role-based permission checks beyond
   this yet.
4. **Observability is a foundation, not a monitoring platform.**
   `GET /health` (liveness) and `GET /readiness` (dependency checks)
   exist on both services; `GET /metrics` gives a plain-JSON counter
   snapshot (not Prometheus format); every Kernel step's latency and
   an error category now reach `agent_run_traces`. No Prometheus,
   Grafana, Kubernetes, alerting, or load testing yet — see
   `docs/OBSERVABILITY.md`.

## Directory map

```
ai-support-saas/
├── node-api/          Business backend (Express/TS)
├── python-api/         AI service (FastAPI) — Kernel lives here
├── db/init/*.sql        Postgres schema, applied in filename order on first boot
├── docker-compose.yml    postgres + redis + python-api + node-api
├── web/                  Empty scaffold — no frontend built yet
├── docs/                 Detailed docs (this file's table above)
└── PROJECT_STATUS.md     Implemented / Partial / Planned, by area
```
