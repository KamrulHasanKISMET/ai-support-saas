# docs/MEMORY.md

**Files:** `python-api/app/memory/memory_service.py` (write path),
`memory_search.py` (read path). **Table:** `customer_memories`
(`db/init/003_knowledge_rag.sql`).

## Write path — `memory_service.extract_and_store()`

Called from `Kernel.run()` step 7, **after** the reply has already been
generated (its failure can't block the reply — isolated in its own
try/except in `kernel.py`).

```
message
  → ai_service.complete_json(MEMORY_EXTRACTION_PROMPT)
  → {"memories": [{"key": ..., "value": ..., "confidence": ...}, ...]}
  → for each proposal:
        skip if key or value missing
        skip if confidence < 0.55  (MIN_CONFIDENCE_TO_STORE)
        else INSERT INTO customer_memories (..., memory_type='long_term', ...)
  → commit once at the end (only if at least one row was inserted)
```

**The LLM never writes to the database.** It proposes; this function
validates (presence + confidence threshold) and is the only code path
that performs the `INSERT`.

Note: `memory_type` is **hardcoded to `'long_term'`** for every write.
The column supports `short_term | long_term | semantic | decision |
episodic | business` per the schema comment, but the application only
ever produces `'long_term'` rows — the other types are schema-only,
unused.

## Read path — `memory_search`

- **`get_all(db, tenant_id, customer_id)`** — plain SQL, `SELECT *`
  equivalent ordered by `updated_at DESC`, no limit, no relevance
  ranking. This is the one actually called, from
  `context_engine.assemble()`.
- **`semantic_search(db, tenant_id, customer_id, query_embedding, ...)`**
  — embedding-based similarity search against `customer_memories.embedding`.
  **Defined but never called anywhere in the codebase.** Also, no code
  path currently ever populates `customer_memories.embedding` (writes
  never set it), so even if it were called, every row would have
  `embedding IS NULL` and match nothing.

## Fixed: writes and reads are now independent of RAG's success/failure

`context_engine.assemble()` used to call `hybrid_search.search()` in
the same function body as the Memory fetch, with no isolation between
them:

```python
async def assemble(self, db, tenant_id, customer_id, question, state, retrieval_query=None):
    memories = await memory_search.get_all(db, tenant_id, customer_id)   # succeeds
    candidates = await hybrid_search.search(db, tenant_id, search_query)  # used to raise unconditionally
    ...
    return AssembledContext(question=question, memories=memories, ...)   # never reached on RAG failure
```

Because `hybrid_search.search()` used to raise unconditionally (no
embedding provider wired in), the function raised before it could
`return` — discarding the already-fetched `memories` along with it.
`Kernel.run()` would catch the exception and build a fresh
`AssembledContext` with **empty** memories, even though real memories
existed and had already been successfully fetched moments earlier.

**This is fixed.** `assemble()` now wraps only the RAG call in its own
try/except:

```python
memories = await memory_search.get_all(db, tenant_id, customer_id)
try:
    candidates = await hybrid_search.search(db, tenant_id, search_query)
    top_chunks = reranker.rerank(candidates)
except Exception:
    logger.error(...)
    top_chunks = []
return AssembledContext(question=question, memories=memories, knowledge_chunks=top_chunks, state=state)
```

Memory now reaches the LLM's context regardless of whether RAG
succeeds, fails, or (currently) just returns zero results because
`knowledge_chunks` is empty — see `docs/RAG.md`.

## Confidence

`MIN_CONFIDENCE_TO_STORE = 0.55` is a class constant on
`MemoryService`, not currently configurable per tenant (unlike
`intent_confidence_min`, which reads from `Settings`/`agent_configs`
conceptually — though `agent_configs` isn't actually read either, see
`PROJECT_STATUS.md`).
