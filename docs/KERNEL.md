# docs/KERNEL.md

**File:** `python-api/app/kernel/kernel.py` — single class `Kernel`, one
public method `run()`. Called only from `CoreAgent.run()` (see
`docs/AGENT.md`) — never called directly from a route.

## Exact lifecycle (verified line-by-line against the code)

```
run(db, tenant_id, customer_id, conversation_id, message, request_id=None)
│
├─ 1. LANGUAGE   language_engine.understand(message)
│                 → LanguageResult(original_message, detected_language,
│                    reply_language, normalized_message, confidence,
│                    communication_style, is_ambiguous, ambiguity_reason,
│                    entity_spans)  ← v2 fields, see docs/LANGUAGE.md
│                 NOT individually try/excepted.
│                 Logged in two lines: core fields, then the new
│                 structured fields (style/ambiguity/entity_spans).
│
├─ 2. INTENT     intent_engine.classify(normalized_message,
│                    entity_hints=language_result.entity_spans)
│                 ← uses the NORMALIZED message, not the raw one
│                 ← entity_spans passed as an ADVISORY hint only —
│                    Intent Engine still owns all entity typing/labeling
│                 NOT individually try/excepted.
│
├─ 3. STATE      state_engine.update_state(db, tenant_id, conversation_id,
│                    intent_result)
│                 upserts conversation_states from intent_result.entities
│
├─ 4. DECIDE (confidence gate)
│    if intent_result.confidence < settings.intent_confidence_min (default 0.60):
│        → ai_service.complete(message, system=CLARIFICATION_SYSTEM_PROMPT
│              + "Reply in this language: {reply_language}")
│        → build KernelRunResponse, SKIP straight to step 7 (still runs)
│    else:
│        → continue to step 5
│
├─ 5. CONTEXT (only in the "else" branch)
│    try:
│        context_engine.assemble(db, tenant_id, customer_id,
│            question=message,              ← ORIGINAL message shown to LLM
│            state=state,
│            retrieval_query=normalized_message)  ← NORMALIZED message used for RAG
│    except Exception:
│        log error, context = AssembledContext(question=message, state=state)
│        ⚠️ this empty fallback has EMPTY memories too — see docs/RAG.md
│           for why this happens on every call right now
│
├─ 6. REASON
│    if intent in (CREATE_ORDER, ORDER_STATUS):
│        log "Tool not yet implemented, falling back to plain answer"
│        (no actual tool call happens — this is a log line only)
│    reply = ai_service.complete(context.to_prompt_block(),
│                system=RESPONSE_SYSTEM_PROMPT + reply-language instruction)
│
├─ 7. LEARN (runs in BOTH branches — clarification and normal)
│    try:
│        memory_service.extract_and_store(db, tenant_id, customer_id, message)
│              ← uses the ORIGINAL message, not normalized
│    except Exception:
│        log error only — never affects the reply already built
│
└─ return KernelRunResponse(reply, intent, confidence, state, toolsCalled,
                             detectedLanguage, replyLanguage,
                             normalizedMessage, decision, retrievalUsed,
                             retrievalChunkCount, errorOccurred)
   ← the last 5 fields were added for Agent Run Trace (docs/AGENT.md);
     all optional/defaulted, none of them changed anything above this line

OUTER try/except wraps ALL of the above:
    on ANY unhandled exception (steps 1, 2, 3, or an uncaught error in 4/6):
        → return KernelRunResponse(reply=FALLBACK_REPLY, intent=None,
              confidence=0.0, state={}, toolsCalled=[],
              decision="fallback", errorOccurred=True)
          (detectedLanguage/replyLanguage/normalizedMessage are NOT set
           on this path — they stay None, since the exception can occur
           before language_result even exists)
```

## Error-handling boundaries — what's isolated vs. not

| Failure point | Isolated? | Effect |
|---|---|---|
| Language Engine | ❌ No | Falls to outer catch → generic fallback reply for the whole turn |
| Intent Engine | ❌ No | Same as above |
| Context/RAG assembly | ✅ Yes | Degrades to empty context, reply still generated (see RAG.md caveat) |
| Memory extraction | ✅ Yes | Never blocks the reply |
| Final LLM reasoning call | ❌ No | Falls to outer catch |

If you're adding a new step, decide deliberately whether it needs its
own try/except (like RAG/Memory) or whether an outer-catch fallback is
acceptable (like Language/Intent currently).

## What is NOT implemented here

- **Tool Engine**: `CREATE_ORDER`/`ORDER_STATUS` intents are detected
  but never trigger a real tool call — just a log line, then a normal
  (ungrounded-in-real-order-data) LLM answer.
- **Decision Engine**: no business-rule-aware routing exists. The
  `business_rules` table is never read.
- **Negotiation**: `NEGOTIATION` is a valid `IntentType` but has no
  special handling — it's answered like any other intent.
- **Human escalation**: no hook exists anywhere in the Kernel.

## System prompts (verbatim, for reference)

Two prompts live as module-level constants in `kernel.py`:
`RESPONSE_SYSTEM_PROMPT` (grounds the answer in memory/state/knowledge,
forbids inventing prices/stock/orders) and `CLARIFICATION_SYSTEM_PROMPT`
(used only on the low-confidence path). Both get
`\n\nReply in this language: {reply_language}.` appended at call time —
this is NOT baked into the constants, it's concatenated per-request in
`kernel.run()`.
