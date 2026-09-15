# docs/RAG.md

**Files:** `python-api/app/rag/search.py` (vector + keyword search),
`hybrid_search.py` (combine), `reranker.py`, `context_builder.py`,
`app/ai/embedding_service.py` (embedding provider).
**Tables:** `knowledge_documents`, `knowledge_chunks` (`vector(1536)`
column) in `db/init/003_knowledge_rag.sql`.

## Current runtime status: runs cleanly, retrieves nothing

`embedding_service.py` now calls OpenAI's `text-embedding-3-small`
(1536 dimensions, matching the schema's `vector(1536)` columns) via
`AsyncOpenAI`, using `OPENAI_API_KEY` from `python-api/.env`. The
client is constructed lazily (on first use, not at import time), so a
missing/empty key doesn't crash the service at startup -- it only fails
when an embedding is actually requested, and that failure is caught
(see below).

**But there is still no knowledge ingestion pipeline.** Nothing in
`node-api` or `python-api` has ever inserted a row into
`knowledge_documents` or `knowledge_chunks`. So even with a working
embedding provider, `hybrid_search.search()` runs without error and
simply returns an empty candidate list -- there's nothing in the tables
to find. Building the ingestion pipeline (upload -> clean -> chunk ->
embed -> store) is `docs/ROADMAP.md` item 2 and is required before RAG
does anything useful.

## Fixed: RAG failure no longer discards Memory

Previously `hybrid_search.search()` raised `NotImplementedError`
unconditionally (no provider wired up), and because
`context_engine.assemble()` called it in the same function body as the
Memory fetch, a RAG failure raised the whole function before it could
return -- discarding memories that had already been fetched
successfully. `assemble()` now wraps the RAG call in its own
try/except; a RAG failure (missing API key, network error, rate limit,
anything) only empties `knowledge_chunks`. Memory is fetched and
returned independently. See `docs/MEMORY.md`.

## Pipeline (as it actually runs today)

```
question (normalized by Language Engine)
   |
 -------------------------
 |                       |
vector_search        keyword_search
(pgvector cosine,     (Postgres full-text,
 needs OpenAI          needs no embeddings)
 embeddings)
 |                       |
 -------------------------
         |
   combine + dedupe by chunk id (hybrid_search.py)
         |
   reranker.py: score = 0.7*vector_score + 0.3*keyword_score
                top_k=5, min_score=0.15
         |
   context_builder.py: "[1] ...\n[2] ..." formatted block
         |
   fed into AssembledContext.to_prompt_block() -> LLM
```

Both branches run today (no exception), but both return empty results
against an empty `knowledge_chunks` table.

## To make RAG actually useful

1. Set `OPENAI_API_KEY` in `python-api/.env` (required -- without it,
   `embed()` will fail at call time with an auth error, caught
   gracefully but producing no results either way).
2. Build a knowledge ingestion pipeline -- still entirely unbuilt.
   Needs, at minimum: a route to accept a document, a chunker, a call
   to `embedding_service.embed_batch()` (already built for batch use --
   don't call `embed()` in a loop), and inserts into
   `knowledge_documents`/`knowledge_chunks`.

## Debug endpoint

`GET /rag/search?tenantId=&q=` (`app/api/routes/rag.py`) -- runs hybrid
search + rerank directly, bypassing the Kernel. Now requires the
`x-internal-secret` header (see `docs/API_CONTRACTS.md`) like every
other `python-api` route except `/health`. Useful for testing
retrieval quality once the ingestion pipeline exists.
