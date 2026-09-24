# docs/CONTEXT_ENGINE.md

**File:** `python-api/app/context/context_engine.py` — same file, same
`ContextEngine` class, same `assemble()` signature and call site
(`kernel.py` step 5) as before this phase. Nothing was replaced,
renamed, or moved. **Tests:** `python-api/tests/test_context_engine.py`
(22 tests).

## Where this sits (unchanged hierarchy)

```
CoreAgent.run()
  → Kernel.run()
    → ContextEngine.assemble()   ← this document
      → Memory (memory_search.get_all)
      → RAG (hybrid_search.search)
```

`ContextEngine` is a capability the Kernel calls, not a replacement
for the Kernel or CoreAgent. The Kernel remains responsible for the
lifecycle (Language → Intent → State → **Context** → Reasoning →
Memory Update), tenant/customer identifiers, and error boundaries;
`ContextEngine` remains responsible only for turning "memory + RAG +
state" into one validated, budget-respecting prompt block.

## The pipeline (Commercial V1 — this phase)

```
retrieve → filter/rank/select → budget → optional compression → validate → build
```

`retrieve`/`filter`/`rank`/`select` are the **same, unchanged** logic
that existed before this phase (`memory_search.get_all()`,
`hybrid_search.search()`, `reranker.rerank()` — see `docs/RAG.md`,
`docs/MEMORY.md`). `budget`, `compression`, and `validate` are new
stages this phase adds, each as a private method on the same class:

| Stage | Method | What it does |
|---|---|---|
| Budget | `_apply_budget()` | Keeps items in their existing priority order (memories: most-recently-updated first; knowledge chunks: highest-relevance first, per the reranker's own ordering) and drops **whole** low-priority items once a token budget is exhausted. Never truncates a single memory or chunk mid-string. `state` is reserved from the budget first, in full, since it's small and required for slot-filling. |
| Compression | `ContextCompressor` / `NoOpCompressor` | A foundation only — see below. |
| Validate | `_clean_context()` | Removes malformed/duplicate memories and knowledge chunks, flags an empty/invalid question, recovers from a non-dict `state` instead of crashing. |
| Tenant scope | `_validate_tenant_scope()` | Runs **before** any retrieval — refuses to even attempt a query with a missing/invalid `tenant_id`/`customer_id`. |

## Context Budgeting

Configurable via `settings.context_token_budget`
(`app/core/config.py`; `assemble(..., token_budget=...)` can override
it per-call, though no caller does today — `kernel.py` uses the
default). Token counts are a rough estimate (`len(text) // 4`), not an
exact tokenizer count — deliberately avoids adding a tokenizer
dependency for a budget that only needs to be approximately right.

**Priority order, and why:** `state` (always kept, reserved first) >
memories (in their existing recency order) > knowledge chunks (in
their existing relevance order, from the reranker). This means the
*first* things dropped under a tight budget are the **lowest-relevance
knowledge chunks** — never a customer fact, never conversation state,
and never a memory ahead of a less-relevant chunk. `budget_info`
(returned on `AssembledContext`) reports `tokensBudget`, `tokensUsed`,
`memoriesKept`/`memoriesDropped`, `chunksKept`/`chunksDropped` — this
is what reaches the trace (see "Observability" below).

## Context Compression — foundation only, not implemented

Per the task constraint ("do NOT build a complex autonomous
summarization system yet"), this phase adds only the **shape** a real
compressor would need to fit into, as an injectable strategy:

```python
class ContextCompressor:
    def compress(self, memories, knowledge_chunks, budget_info) -> tuple[list[dict], list[dict], bool]:
        raise NotImplementedError

class NoOpCompressor(ContextCompressor):
    def compress(self, memories, knowledge_chunks, budget_info):
        return memories, knowledge_chunks, False  # current production behavior
```

`ContextEngine(compressor=...)` accepts any `ContextCompressor`; the
default (and only implementation that exists today) is `NoOpCompressor`
— compression is **off** in production right now, by construction, not
by a feature flag that could be misconfigured. A future real
implementation (e.g. an LLM-based summarizer for `knowledge_chunks`)
is a constructor argument swap, not a change to `assemble()`'s control
flow or to `kernel.py`'s call site. The contract such an implementation
must uphold (stated in the base class's docstring): preserve critical
facts, instructions, tenant/store identity, security context, and
important tool results.

## Context Validation

Two independent checks, both fail-*safe* (never raise, never crash the
turn):

1. **Tenant/customer scope** (`_validate_tenant_scope`) — runs first,
   before any query. If `tenant_id`/`customer_id` aren't positive
   integers, `assemble()` returns immediately with
   `memory_status="skipped"`, `rag_status="skipped"`, and
   `validation_errors=["invalid_tenant_or_customer_scope"]` — **no
   retrieval is attempted**. This is defense-in-depth at the boundary
   `ContextEngine` actually controls (its own inputs); every
   underlying query is already tenant-filtered in its own SQL
   (`memory_search.py`, `rag/search.py` — unchanged, not touched by
   this phase). **Scope of this guard, stated precisely:** it validates
   the *inputs* to `assemble()`, not each individual row a query
   returns — `memory_search`/`rag/search` results don't currently carry
   their own `tenant_id` per row to cross-check against (adding that
   would touch two more files; deferred, see "Remaining gaps" in the
   final report for this phase).
2. **Structural cleanliness** (`_clean_context`) — after budgeting:
   drops non-dict entries, entries missing required fields
   (`memory_key`/`memory_value` for memories, non-empty `content` for
   chunks), and exact duplicates. Runs **after** budgeting/compression
   so nothing either of those stages could introduce slips through
   unchecked. Always returns a usable (possibly empty) context —
   `to_prompt_block()` already renders "None known yet." /
   "No relevant knowledge found." for empty memories/chunks, unchanged
   from before this phase.

## Memory/RAG failure visibility (also new this phase)

Previously, a Memory or RAG retrieval failure degraded gracefully
(empty results) but was logged and then **invisible** at the trace
level. `assemble()` now tracks `memory_status`/`memory_error` and
`rag_status`/`rag_error` independently — one failing never blocks the
other (unchanged behavior; only the reporting is new). Error text goes
through `app/core/trace_sanitize.py`'s `safe_error_message()` before
being stored anywhere — see `docs/AGENT.md`'s "Commercial V1 lifecycle
hardening" section and `docs/OBSERVABILITY.md` section 8.

## Observability — recorded in the existing trace, no new infrastructure

`kernel.py` folds `AssembledContext`'s new fields into the **same**
`steps` list it already builds for the Agent Run Trace (see
`docs/AGENT.md`) — no second observability system:

- A `"context"` step entry, `metadata: {budget, compressionApplied,
  validationErrors}`.
- Separate `"memory"` and `"rag"` step entries, each with its own
  `status`/`durationMs`/`error`.

Nothing here required a new table beyond `008_trace_lifecycle.sql`'s
`steps` JSONB column (already needed for the broader lifecycle work —
see `docs/AGENT.md`).

## What this phase explicitly did NOT do

- No autonomous/LLM-based summarization (`NoOpCompressor` is the only
  implementation).
- No change to `Kernel.run()`'s control flow, error boundaries, or
  prompts.
- No change to `memory_search.py`/`rag/search.py` (the compression and
  budgeting stages operate on their existing output shape unchanged).
- No new framework, no new dependency.
- No read/query API for budget/validation history — it's in the trace,
  query it with SQL like everything else there.
