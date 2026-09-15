# docs/LANGUAGE.md

**Files:** `python-api/app/language/language_engine.py`,
`language_types.py`. Runs as **step 1** inside `Kernel.run()`, before
Intent classification. **Tests:** `python-api/tests/test_language_engine.py`
(14 tests, all passing — run with
`docker compose exec python-api python -m unittest tests.test_language_engine -v`).

## v2: structured semantic understanding (this version)

The Language Engine still makes exactly **one LLM call** (no new call
added), but that call's JSON schema was extended so its output is a
richer, structured signal that Intent/RAG/Context/Kernel can safely
consume — without Language Engine taking over their jobs.

```json
{
  "detectedLanguage": "bn" | "en" | "mixed" | "other",
  "replyLanguage": "bn" | "en" | "other",
  "normalizedMessage": "...",
  "confidence": 0.0-1.0,
  "communicationStyle": "formal" | "informal" | "urgent" | "polite" | "neutral",
  "isAmbiguous": true | false,
  "ambiguityReason": "short reason, or null",
  "entitySpans": ["substring1", "substring2", ...]
}
```

`LanguageEngine.understand(message)` parses this into a `LanguageResult`
dataclass:

```python
@dataclass
class LanguageResult:
    original_message: str          # NEW — see "original message" section below
    detected_language: str
    reply_language: str
    normalized_message: str
    confidence: float
    communication_style: str = "neutral"      # NEW
    is_ambiguous: bool = False                # NEW
    ambiguity_reason: str | None = None       # NEW
    entity_spans: list[str] = field(default_factory=list)  # NEW
```

Every new field has a safe default and defensive parsing — a missing,
malformed, or unexpected-type value from the LLM never raises; it
falls back (e.g. non-list `entitySpans` → `[]`, non-string items in the
list are filtered out, missing `communicationStyle` → `"neutral"`).
See `TestDefensiveParsing` in the test file for exact coverage.

## What each new field is for, and its current consumer

| Field | Purpose | Consumed by (this version) |
|---|---|---|
| `original_message` | Makes the result object self-contained — a consumer holding just a `LanguageResult` can see what was originally said, without needing the raw `message` threaded to it separately. **Never used to overwrite anything** — see below. | Nothing yet; available for future consumers. Kernel still keeps its own `message` variable as the source of truth for storage/memory/prompt-display, same as before. |
| `communication_style` | Coarse tone/register signal. | **Logged only** (`Kernel.run()` step 1). Not yet used to adjust reply tone — that's future scope, not this increment. |
| `is_ambiguous` / `ambiguity_reason` | Flags messages whose meaning depends on context the Language Engine cannot see (most commonly short follow-ups with an unresolved pronoun — "oita koto?" / "ওইটা কত?"). This is a **signal**, not a decision. | **Logged only**. The Kernel's confidence gate (`settings.intent_confidence_min`) is unchanged in this version — a future Decision Engine increment can choose to act on `is_ambiguous` (e.g. always clarify regardless of Intent confidence). Deliberately not wired to any behavior change yet, to keep this increment's risk minimal. |
| `entity_spans` | Untyped candidate entity substrings, copied verbatim from `original_message` (not translated) — **boundary only**, never labeled/typed. | **Consumed**: passed to `intent_engine.classify(normalized_message, entity_hints=entity_spans)` as an advisory hint. Intent Engine still does 100% of the actual typing/labeling into its `entities` dict — hints can be confirmed, retyped, ignored, or extended; they are never copied directly into the result (`TestIntentEngineEntityHints.test_intent_engine_still_owns_entity_typing` asserts this explicitly). |

`normalized_message` is unchanged in meaning and role — it remains the
"semantic meaning" representation (a short English paraphrase). No
separate/duplicate field was added for this; the requirement was
already satisfied by the existing field.

## Original message: what "preserved" means precisely

`original_message` (inside `LanguageResult`) and the separate `message`
variable that `Kernel.run()` already held are always identical — this
was true before this version too, just implicit rather than explicit.
Verified consumers, unchanged in this version:

| Consumer | Uses `original_message`/`message`, or `normalized_message`? |
|---|---|
| `intent_engine.classify(...)` | **normalized** (as before) |
| `context_engine.assemble(..., retrieval_query=...)` → RAG search query | **normalized** (as before) |
| `context_engine.assemble(..., question=...)` → shown to LLM as "CUSTOMER QUESTION" | **original** (as before — including the empty-context fallback path) |
| `memory_service.extract_and_store(...)` | **original** (as before) |
| Stored in Postgres (`messages` table, via node-api) | **original** — Python never sees or affects this |

`normalized_message` is never written back into `original_message`,
and nothing in this version changes what gets stored, shown to the
customer, or fed to Memory extraction.

## Intent Engine: new optional `entity_hints` parameter

```python
async def classify(self, message: str, entity_hints: list[str] | None = None) -> IntentResult:
```

Backward compatible: omitting `entity_hints` (or passing `None`/`[]`)
produces byte-for-byte the same prompt as before this change — verified
by `test_no_hints_produces_no_hint_block_backward_compatible` and
`test_empty_hints_list_also_produces_no_hint_block`. When hints are
given, one advisory paragraph is added to the prompt, explicitly
labeled "unverified, untyped" and telling the model it may "confirm,
retype, ignore, or extend" them — Intent Engine remains the sole
authority on the final `entities` dict.

## Kernel integration (the only two behavior changes in `kernel.py`)

1. `intent_engine.classify(normalized_message)` → 
   `intent_engine.classify(normalized_message, entity_hints=language_result.entity_spans)`
2. One additional `logger.info(...)` line logging
   `communication_style`, `is_ambiguous`, `ambiguity_reason`, `entity_spans`.

Nothing else in the Kernel's control flow, confidence gate, system
prompts, or error-handling boundaries changed. `docs/KERNEL.md`'s
lifecycle diagram and error-boundary table are still accurate as
written — Language Engine is still NOT individually try/excepted (a
failure still falls through to the Kernel's outer catch), which is
unchanged from before this version.

## Known limitations (unchanged from before, still true)

- **Not individually error-isolated** in the Kernel — a Language Engine
  failure still falls through to the generic fallback reply.
- No caching — every message gets a fresh call.
- No confidence gate on `detected_language`/`reply_language`/`is_ambiguous`
  — these are logged but don't trigger special handling yet.
- **No conversation history is passed to the Language Engine.** This is
  intentional for this increment (the task explicitly excludes
  "advanced identity resolution"). This means short contextual
  follow-ups are correctly flagged as `is_ambiguous=True` rather than
  guessed at — the Language Engine honestly reports "I can't resolve
  this alone" instead of hallucinating what a pronoun refers to. Actually
  *resolving* such follow-ups against `conversation_states` (which
  already holds prior-turn slots) is a natural next increment, not done here.
