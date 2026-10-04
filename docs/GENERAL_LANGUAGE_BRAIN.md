# docs/GENERAL_LANGUAGE_BRAIN.md

**This is the single source of truth for the Own General Language
Brain architecture.** It supersedes the framing (not the file) of
`docs/LANGUAGE_INTELLIGENCE.md`: that file remains the repo-side
*implementation tracker* (what's built, what test to run, what SQL to
check) — this file is the *architecture and terminology* it must stay
consistent with. Where the two ever disagree, this file wins and
`LANGUAGE_INTELLIGENCE.md` must be corrected to match.

Read this after `AI_CONTEXT.md` and `PROJECT_STATUS.md`, before
touching anything under `app/language/`.

---

## 0. What changed from the existing design, and why

The Phase 0–4 work already built is **preserved in full** — nothing
here discards it. It is re-labeled and extended, not replaced:

| Existing piece | Old label | Corrected label | Why |
|---|---|---|---|
| `shadow_brain.py` + `intent_clusters` (nearest-example matcher) | "the Language Brain" | **Intent Understanding** — one capability *inside* the General Language Brain | An embedding-nearest-neighbor intent matcher is real and worth keeping, but it is not general language understanding. It answers "which of N known intents is this closest to," not "what does this mean." Calling it the finished Brain forecloses the rest of the architecture. |
| `language_experiences.brain_prediction` (LLM's `final_intent` used directly as the label a cluster is built from) | implicit ground truth | **candidate interpretation**, promoted to a trusted learning signal only through the evidence hierarchy (§2) | `final_intent` today is "whatever the Intent Engine LLM call returned," with no verification step before it feeds `ClusterBuilderService`. That is LLM-agreement-as-ground-truth, exactly what Correction #2 forbids. |
| `learning_eligible` flag, `_LEARNING_INELIGIBLE_INTENTS` / `ROUTING_INELIGIBLE_INTENTS` (both currently `{CREATE_ORDER, ORDER_STATUS}`, and currently identical) | one flag doing two jobs | **split into `language_learning_eligible` and `automation_eligible`**, two separate control planes (§3) | The repo today conflates "safe to learn the language pattern from" with "safe to let the Brain act on autonomously." A message like *"amar parcel ta koi?"* is excellent language-learning material and simultaneously unsafe to auto-resolve — the current single flag cannot express that. |
| Fixed `detectedLanguage: "bn" \| "en" \| "mixed" \| "other"` enum in `language_engine.py`'s JSON schema | closed language list | **open-set language tag + separate script/transliteration fields** (§5) | The enum is a ceiling today, not just a set of examples — anything the LLM tags outside those four values is silently the `"other"` bucket with no room to grow. |
| Phase 0–4 roadmap (`docs/LANGUAGE_INTELLIGENCE.md`) | terminal at Phase 4 | **extended to Phase 8** (§7) | Phase 4 as written stops at "scale the shadow matcher across tenants." It never reaches an own model, a verified-evidence learning loop, or a stated cost KPI — all mandatory commercial requirements here. |

Per-phase KEEP/MODIFY/MERGE/SPLIT/RENAME/ADD/REMOVE verdicts are in §7.2.

---

## 1. What the General Language Brain is, and is not

**General Language Brain (GLB):** the architectural layer, spanning
several concrete components, that progressively learns to map

```
human language surface
  → natural meaning
    → semantic representation
      → contextual meaning
        → customer goal
          → downstream structured understanding (intent, entities, state)
```

from real, verified interaction data — with decreasing reliance on the
external LLM for the patterns it has already learned well.

**It is not:**

- A single model or a single file. It is a layer composed of several
  narrower capabilities (below).
- The current `shadow_brain.py` nearest-example matcher, on its own.
  That matcher is real, kept, and important — it is the **foundation
  implementation of one GLB capability (Intent Understanding)**, not
  the GLB itself.
- A system that stops calling the LLM. The GLB's job is to shrink the
  LLM's role to *teacher, fallback, novel-case solver, and evaluator* —
  never to eliminate it (see the non-negotiable final principle, §11).
- A justification for autonomous business action. Understanding
  language and being authorized to act on it are different questions
  (§3).

### 1.1 The layer decomposition (Correction #1)

The GLB is the umbrella over these distinct capabilities. Existing
modules are mapped in; unbuilt ones are marked:

| Capability | What it answers | Current implementation | Status |
|---|---|---|---|
| **A. General Language Brain** | the umbrella — orchestrates B–M, owns the evidence hierarchy and the learning loop | none yet as an umbrella; today B stands in for the whole thing | this doc defines it; §7 phases it in |
| **B. Intent understanding** | "which structured intent does this serve?" | `intent_engine.py` (LLM) + `shadow_brain.py`/`intent_clusters` (own matcher) | built, mislabeled as "the Brain" — relabel only |
| **C. Entity understanding** | "what specific things (product, order id, quantity...) are named?" | `intent_engine.py` typed `entities` dict, with `language_engine.py`'s `entity_spans` as an untyped hint | built |
| **D. Context understanding** | "what does this mean given the conversation so far?" | `context_engine.py`, `conversation_states` | built (assembly/budgeting); no pronoun/ellipsis resolution yet — `is_ambiguous` is flagged, not resolved (see `docs/LANGUAGE.md`) |
| **E. Customer memory** | "what do we already know about this specific customer?" | `memory_service.py`, `customer_memories` | built |
| **F. Business knowledge** | "what does this tenant's catalog/policy say?" | RAG (`docs/RAG.md`), `business_rules` | RAG built but unpopulated (no ingestion pipeline); `business_rules` unread by Kernel |
| **G. Tenant Brain** | "what is this tenant configured to do?" | `agent/tenant_brain.py` | foundation only, unwired |
| **H. RAG** | retrieval over F | `rag/` (search, hybrid_search, reranker, context_builder) | built, empty corpus |
| **I. Reasoning** | combining B–H into a course of action | implicit inside the single LLM prompt in `kernel.py` | not a separate component today |
| **J. Decision-making** | should the system act, ask, or escalate? | the Kernel's confidence gate (`intent_confidence_min`) only | minimal; no Decision Engine yet (`docs/ROADMAP.md` §3) |
| **K. Tools/actions** | executing a business action | not built | `ROADMAP.md` §3 marks the insertion point |
| **L. Response generation** | producing the customer-facing reply | the LLM call inside `kernel.py` | built **via the external LLM only — no own-model path was ever designed; added as Phase 6B (2026-09-29), see `PHASE_5_8_PLAN.md`** |
| **M. Evaluation/learning** | did B–L do the right thing, and should that update anything? | `calibration_service.py`, `promotion_service.py`, `novelty_detector.py` | built for B (intent) only; §2 generalizes it |

The Phase 0–4 work is entirely inside **B** and its evaluation loop
(**M**, scoped to B). That is genuinely valuable and is preserved
without modification to its working code — it is simply no longer
allowed to be *described* as the finished GLB in documentation. Update
every doc that currently reads:

```
# Current semantic/intent brain
FINAL GENERAL LANGUAGE BRAIN
```

to:

```
# Current semantic/intent brain
FOUNDATION OF THE OWN LANGUAGE BRAIN — Intent Understanding (capability B)
```

(`docs/LANGUAGE_INTELLIGENCE.md`'s title/intro is the primary place
this must change — see §7.2, Phase 1/2/3 rows.)

---

## 2. Evidence hierarchy (Correction #2)

### 2.1 The chain

```
Customer message
   ↓
Own Brain prediction              (capability B, calibrated similarity)
   ↓ (if below threshold / novel / high-risk)
LLM interpretation                ← CANDIDATE, not truth
   ↓
Verification / evaluation         (see 2.2 — may be immediate or deferred)
   ↓
Real outcome / human feedback / trusted business signal
   ↓
Learning signal                   ← only NOW eligible to update anything
```

`final_intent` in `language_experiences` today *is* the LLM
interpretation — that part of the pipeline is correctly built. What is
missing is everything from "Verification" onward: nothing today
distinguishes a `final_intent` that was later contradicted by the
customer, overridden by a human agent, or connected to a failed order
from one that was quietly correct. `ClusterBuilderService` currently
treats every `learning_eligible=TRUE` row identically, regardless of
whether any of that verification ever happened. That is the literal
"LLM says X → automatically teach Brain X" pattern Correction #2
prohibits.

### 2.2 Verification levels (new — data model addition)

Add a `verification_level` enum, replacing the current
always-`'unverified'` `verification_result` column with a real state
machine:

| Level | Meaning | Example source |
|---|---|---|
| `unverified` | recorded, nothing has checked it | default at write time (unchanged from today) |
| `self_consistent` | Own Brain and LLM agreed | `shadow_brain` agreement, already computed, just not yet used as a level |
| `outcome_positive` | a downstream business signal confirms it worked | order actually completed matching the predicted intent; customer did not follow up with a correction within N turns |
| `outcome_negative` | a downstream signal contradicts it | customer rephrased/complained; a human agent overrode the intent; the resulting action failed |
| `human_confirmed` | a human explicitly labeled it correct | novelty-triage endpoint (`ROADMAP.md` admin routes), or a support agent's correction UI (not yet built) |
| `human_corrected` | a human explicitly relabeled it | same UI, different outcome — this is a **label revision**, and the old label must be superseded, not deleted (audit trail, §2.4) |

### 2.3 Learning eligibility gate

A row becomes an **eligible learning signal** — usable by
`ClusterBuilderService`, or by any future component that trains
anything — only when:

```
verification_level IN ('outcome_positive', 'human_confirmed')
  OR (verification_level = 'self_consistent' AND source_reliability >= tenant_config.min_reliability)
```

`self_consistent` alone (own Brain and LLM agreeing) is allowed to
count as *provisional* evidence — this is how the system bootstraps
before enough outcome/human signal exists — but a tenant can require
stricter evidence (`min_reliability`) the same way
`tenant_calibration_config` already lets a high-stakes tenant demand
`min_agreement_rate=0.95` today. This is an additive column, same
per-tenant-override pattern already in use — no new mechanism class.

### 2.4 Disagreement, conflicting labels, revision, rollback

- **Disagreement handling**: when Own Brain and LLM disagree, the
  Brain's prediction is *not* discarded — it is logged
  (`brain_prediction->>'agreement' = false`, already captured today)
  and treated as a **novelty/uncertainty signal** feeding
  `novelty_detector.py`, not silently overwritten by the LLM.
- **Conflicting labels**: if two verified signals for the same
  message/cluster disagree (e.g. `human_confirmed` intent A today,
  `human_corrected` to intent B next week), the newer verified label
  wins for future learning, but the old row is retained with a
  `superseded_by` pointer — never deleted. This is the same append-
  only philosophy `cluster_promotion_log` already uses for promotion
  events; extend it to label revisions.
- **Label revision / rollback**: exactly the existing
  `PromotionService` canary → promote/rollback lifecycle, generalized:
  a cluster built from since-downgraded evidence (enough
  `outcome_negative` rows accumulate against a promoted cluster) is
  eligible for the same rollback path `check_and_promote()` already
  implements for canary failure — extend its accuracy check to also
  watch post-promotion outcome signals, not just canary-period
  `routing_decisions.was_correct`.
- **Learning audit trail**: `cluster_promotion_log` is already this
  pattern for cluster-level events. Extend the same idea to row-level
  label changes: every verification-level transition and every label
  revision is one immutable log row, never an in-place mutation of
  history.

---

## 3. Two control planes (Correction #3)

**Language understanding and business action authority are separate
control planes.** A message can be fully understood and still be
forbidden from triggering autonomous action. Today's single
`learning_eligible` flag (mirrored into `ROUTING_INELIGIBLE_INTENTS`)
collapses these into one bit. Split into four independent flags,
evaluated per intent (and overridable per tenant, same pattern as
`tenant_calibration_config`):

| Flag | Question it answers | Default for `CREATE_ORDER`/`ORDER_STATUS` | Default for most intents |
|---|---|---|---|
| `language_learning_eligible` | May this message's *language pattern* (surface → semantic mapping) be learned from? | **`TRUE`** — this is the corrected behavior | `TRUE` |
| `automation_eligible` | May the system act on this intent *without a human in the loop*, once confidently understood? | `FALSE` (unchanged — high business risk stays gated) | `TRUE` |
| `promotion_eligible` | May a candidate cluster built from this intent's experiences be promoted into the live `intent_clusters` set (i.e. affect routing) via `PromotionService`? | `TRUE` for the *language pattern*, independent of `automation_eligible` | `TRUE` |
| `training_eligible` | May this experience be used in an offline training/distillation dataset for a future own model (§6)? | `TRUE`, subject to the same tenant-isolation rules as any other data (§4) | `TRUE` |

Worked example, exactly the one in the correction:

> *"amar parcel ta koi?"* (ORDER_STATUS)

- `language_learning_eligible = TRUE` — yes, learn that this phrasing
  means "where is my order," across Banglish generally.
- `automation_eligible = FALSE` — no, do not let the Brain
  autonomously execute an order-status action just because the
  language is understood; a Tool Engine invocation still needs its own
  permission/business-rule check (`ROADMAP.md` §3's
  Propose → Permission Check → Business Rule → Validate → Execute
  pipeline) regardless of how confident the language layer is.
- `promotion_eligible = TRUE` — the routing layer may still use the
  learned pattern to route *understanding* (recognize this as
  ORDER_STATUS quickly, skip an LLM Intent Engine call) even though
  it may **not** use that same confidence to skip the Tool Engine's own
  authorization for the action.
- `training_eligible = TRUE` — usable in a future distilled
  intent/language model's training set (subject to tenant-isolation
  rules, §4).

### 3.1 Required correction to today's code semantics (documentation-level; no code changed in this task)

`core_agent.py`'s `_LEARNING_INELIGIBLE_INTENTS` and
`routing_service.py`'s `ROUTING_INELIGIBLE_INTENTS` are currently the
same frozenset used for two different purposes. The next
implementation step (§9, item 1) is to split this into:

- `AUTOMATION_INELIGIBLE_INTENTS` (today's set, unchanged meaning) —
  keeps `CREATE_ORDER`/`ORDER_STATUS` out of autonomous action.
- Remove the corresponding exclusion from `_LEARNING_INELIGIBLE_INTENTS`
  entirely — `language_experiences.learning_eligible` should be
  `TRUE` for these intents going forward. Rename the column meaning
  (not necessarily the column name, to avoid an unnecessary migration)
  to explicitly mean `language_learning_eligible`.
- `routing_service.py`'s brain-routing decision (whether the Brain may
  answer *understanding* questions fast) may still consult calibrated
  confidence for these intents — routing traffic to the Brain for
  understanding is not the same as authorizing the Brain to act, and
  should no longer be blocked outright the way `ROUTING_INELIGIBLE_INTENTS`
  blocks it today. What must remain blocked, permanently, is the Tool
  Engine (§1.1, capability K) executing `CREATE_ORDER`/`ORDER_STATUS`
  without its own independent authorization step — that gate belongs
  in the not-yet-built Tool Engine, not in the language layer.

This is a genuine behavior change to plan for, not just a rename — it
is called out explicitly here so it is not silently lost when
implementation resumes. It is **not implemented in this task** per the
instruction to update documentation only.

---

## 4. Global vs. tenant vs. customer scope

Three scopes, already implicit in the schema (`tenant_id` on
everything per `docs/ARCHITECTURE.md` rule 1) but not yet explicit as
a learning-scope concept:

| Scope | Contains | Existing tables | Aggregation rule |
|---|---|---|---|
| **Global language knowledge** | general human-language patterns (grammar, common phrasing, code-switching patterns) safe to reuse across all tenants | none yet — today `intent_clusters` is entirely tenant-scoped (`source='tenant_learned'`/`'default_seed'`) | must be built via **privacy-preserving aggregation**: only pattern-level statistics (e.g. "phrasing X commonly maps to goal-shape Y" with no verbatim tenant business terms), never raw tenant messages, promoted into a global table |
| **Tenant-specific language experience** | this tenant's business/domain terminology and customer-language patterns | `language_experiences`, `intent_clusters` (tenant-scoped rows) | never leaves the tenant; this is what's built today |
| **Customer memory** | facts about one specific customer | `customer_memories` | never leaves the customer; already isolated (`docs/MEMORY.md`) |

**Business knowledge must not accidentally become global language
knowledge.** Concretely: `ClusterBuilderService`'s centroid/example
selection today operates entirely within one tenant's rows (correct,
keep). A future global layer must be a *separate*, explicitly-designed
aggregation step — computed statistics only, reviewed for leakage
before promotion to global — never a byproduct of an existing
tenant-scoped job. No global layer exists yet; this section defines
the constraint it must satisfy whenever it's built (§7, Phase 6/7).

---

## 5. Language-agnostic architecture

### 5.1 The problem with today's schema

`language_engine.py`'s JSON schema currently declares:

```json
"detectedLanguage": "bn" | "en" | "mixed" | "other"
```

This is a ceiling: the four literal values are the only thing the
Kernel/Intent Engine can ever see for "what language was this," and
`"mixed"`/`"other"` throw away exactly the information (which
languages, what script, transliterated from what) that matters for a
system meant to generalize to "any human language."

### 5.2 Corrected model

Separate four concepts that are today collapsed into one field:

| Concept | Example values | Notes |
|---|---|---|
| `language` | open string/BCP-47-style tag (`bn`, `en`, `hi`, `ar`, `es`, ...), or a list when genuinely mixed | open set — never a closed enum in the schema or in code (`if language == "bn": ...` is exactly the anti-pattern called out in the correction; keep any such branches to genuinely irreducible cases, e.g. a right-to-left rendering hint, never as the core routing logic) |
| `script` | `latin`, `bengali`, `devanagari`, `arabic`, `cjk`, ... | independent of language — "Banglish"/"Hinglish"/romanized Arabic are Bengali/Hindi/Arabic *language* in *Latin script*, not a fourth language |
| `is_transliterated` | boolean + best-guess source script | lets "price koto?" and "দাম কত?" be recognized as the same language+goal in different scripts, rather than as unrelated buckets |
| `code_mixing` | none / inter-sentential / intra-sentential | replaces the current binary `"mixed"` value with the actual linguistic phenomenon, which downstream generalization (§5.4) needs |

`normalized_message` (the short English paraphrase) is unchanged and
remains the semantic-meaning representation — this schema correction
is additive alongside it, not a replacement.

### 5.3 What must never be inferred

Per the correction: **language, script, or transliteration must never
be used to infer nationality, ethnicity, identity, geography, or
customer location.** This is a hard architectural boundary, not a
tuning knob — no component (Language Engine, Context Engine, Memory,
RAG, any future Decision Engine) may derive or store such an inference
from linguistic signal alone. Existing `customer_memories` extraction
(`docs/MEMORY.md`) already only stores what the LLM is prompted to
extract as explicit customer-stated facts — this boundary must be
carried forward explicitly into that prompt's constraints as the
schema evolves, not left as an accidental byproduct of not having
built it yet.

> **Status (implemented):** the boundary above is now carried into
> `MEMORY_EXTRACTION_PROMPT` (`app/memory/memory_service.py`) as an
> explicit HARD RULE — never infer nationality/ethnicity/religion/
> identity/geography/location from language, script, transliteration or
> style; store such a fact only if the customer states it about
> themselves in words. Pinned by `tests/test_memory_service.py`. Limit:
> this is a prompt-level control (the enforcement point this section
> names); it cannot prove the LLM obeys it and needs evaluation against
> a real model. There is no code-level guard, because a key-name filter
> cannot distinguish an explicitly stated fact from an inferred one.

### 5.4 Phenomena the GLB must progressively handle

The 25-item list in the correction (literal/implied/conversational
meaning, ellipsis, slang, typos, repeated characters, emoji, ASR
artifacts, transliteration, code-switching/mixing, dialect,
paraphrase equivalence, ambiguity, correction, continuity, input/output
language separation) is **not** to be turned into 25 hardcoded schema
fields. The architecture's answer is the same one already partially
built:

- The **LLM Teacher** already provides broad coverage of all 25 today
  (that's what one unconstrained LLM call over natural language does).
- The GLB's job is to progressively identify *which* of these
  phenomena recur often enough, per tenant or globally, to be worth an
  explicit, cheaper own-Brain mechanism — driven by the same
  novelty/confidence data `novelty_detector.py` already collects, not
  by upfront guessing.
- Concretely, today's two explicit fields (`is_ambiguous`,
  `communication_style`) are examples of exactly this: two phenomena
  from the list that were judged worth surfacing structurally. Adding
  the next one (e.g. explicit `is_correction` for "user correction," #23)
  should follow the same bar — recurring, cheaply detectable,
  meaningfully actionable — not an attempt to enumerate all 25 at once.

### 5.5 Semantic generalization (surface variation → shared goal)

The correction's example set (দাম কত? / price koto? / vai eta koto? /
how much is this?) must be read as *illustrative*, not as the ceiling
of what the system recognizes. Architecturally this is the difference
between:

- **Phrase memorization** (what a naive nearest-example matcher risks
  becoming if its example set stays small/verbatim) — recognizing only
  stored strings.
- **Semantic abstraction** (what embeddings + centroid clustering,
  already built in `cluster_builder_service.py`, are supposed to
  achieve) — recognizing the shared goal behind varied surface forms.

The existing centroid/nearest-neighbor design is the right *mechanism*
for this; what's missing is the generalization *test*: before a
cluster is trusted, it should be evaluated against held-out phrasing
it was not built from (§7's multilingual evaluation sets, §2's
verification), not just against agreement with the LLM on its own
training examples. This is the concrete difference between "the Brain
memorized five example sentences" and "the Brain learned the goal."

**Status:** the test exists (`generalization_eval.py`) and is now a
promotion gate: `PromotionService` will not promote a candidate that
cleared the canary accuracy bar but fails held-out generalization
(`held_for_generalization`). Intents without held-out cases skip the
gate; thresholds are uncalibrated and not yet run on real data.

---

## 6. Own model learning path

Five-stage progressive strategy, explicitly provider/model-agnostic
(no specific model technology is assumed or required):

| Stage | Shape | Current repo state |
|---|---|---|
| **1. LLM + retrieval/experience** | every turn calls the LLM; experiences are logged for later use | Phase 0 — done |
| **2. LLM + own semantic components** | an own embedding-based matcher runs alongside the LLM, in shadow | Phase 1 — done (`shadow_brain.py`) |
| **3. LLM + smaller specialized model** | calibrated confidence lets the own matcher *answer* (not just shadow) for high-confidence cases; LLM still called for the rest | Phase 2/3 — done (`routing_service.py`, `promotion_service.py`) for capability B (intent) only |
| **4. Own model handles common cases; LLM handles uncertainty/novelty/complexity** | a **trained/distilled** own model (not just nearest-neighbor over LLM-labeled examples) replaces the matcher for the highest-volume patterns. **This applies to UNDERSTANDING (intent) and, as a separate track, to REPLIES (Phase 6B: approved-reply retrieval first, then a generative model).** | intent side: started (Phase 6). Reply side: **not built, specified in `PHASE_5_8_PLAN.md` Phase 6B** |
| **5. Adaptive model routing** | routing itself becomes a learned policy over confidence/novelty/complexity/risk/cost/latency, not a fixed threshold scan | partially built (`CalibrationService`'s threshold scan is a simple learned policy already); full multi-factor routing is §7 Phase 7 |

Stage 4 is the first stage requiring genuinely new infrastructure
(a training/fine-tuning/distillation pipeline, offline evaluation
sets, model versioning) beyond what exists today. Candidate component
types for Stage 4+ (embedding models, classifiers, rerankers, distilled
or fine-tuned small models, open-source local models) are explicitly
**not** narrowed to one technology in this document — that choice is
deferred to when Stage 4 is actually scoped, based on what the
accumulated verified-experience data actually looks like by then.

---

### 6.0 Teacher→student, depth, and the handover ladder (added 2026-09-29)

The LLM is the teacher of deep language understanding (meaning, context,
implication), not just labels. The own model must learn that depth, not
surface strings, and LLM calls are reduced one channel at a time only
when measured gates pass (until then the LLM keeps doing everything and
every call is a training example). Full plan, depth layers L1–L9 with
their tests, the four LLM channels and the R0–R6 ladder:
`docs/OWN_LANGUAGE_MODEL_TRAINING_PLAN.md`.

### 6.0a Storage-level deduplication (added 2026-09-29)

`language_experiences` today writes one row per turn, no dedup, no
retention. A design for collapsing literal repeats while explicitly
NEVER merging rows by textual/embedding similarity (which would risk
collapsing exactly the minimal pairs §5.4/§5.5 rely on, e.g. negation)
is in `docs/EXPERIENCE_DEDUPLICATION_AND_RETENTION.md`. Not built yet.

### 6.1 Own replies (added 2026-09-29 — a gap in the first version of this doc)

Stages 1–4 above were written about *understanding*. Reply generation
was left with the external LLM by omission. An own model that only
understands still needs the LLM to answer, so the saving is one call
per turn, not the reply. The reply path is therefore its own track
(Phase 6B): (1) capture replies with quality labels, (2) ground answers
in ingested tenant knowledge, (3) an approved-reply retrieval responder
for repetitive low-risk intents, (4) shadow/canary/serving with the same
machinery as intent, (5) only later an own generative model trained on
human-approved/edited replies. The LLM remains teacher, fallback and
handler of novel/ambiguous/complaint/action-authorising turns. Detailed
items and done-criteria: `docs/PHASE_5_8_PLAN.md` (P6R-*).

## 7. Phase model

### 7.1 Final phase sequence

The correction's suggested Phase 0–8 sequence was reviewed against the
dependency structure already implemented in this repo. The repo's
existing Phase 0–4 already interleaves what the suggested sequence
lists as separate Phase 0 (Foundation), Phase 1 (LLM-Powered Core —
already pre-existing before Phase 0 even started here, since the
LLM-only Language Engine predates the whole tracker), Phase 2 (Shadow
Brain), Phase 3 (Confidence + Routing), and Phase 4 (Verified Learning,
partially — cluster building exists, but verification per §2 does
not). Rather than renumber the repo's completed work, this document
**keeps the repo's existing Phase 0–4 numbering** (so
`docs/LANGUAGE_INTELLIGENCE.md` does not need every historical
reference renumbered) and appends the correction's remaining scope as
Phases 5–8, with Phase 4 explicitly extended to include the
verification/evidence work §2 requires before it can be called
complete:

| Phase | Objective | Status |
|---|---|---|
| **Phase 0 — Foundation + Experience Capture** | Log every Language Engine call, no behavior change | ✅ done |
| **Phase 1 — Shadow Intent Matcher** | Own embedding matcher runs alongside LLM, shadow-only | ✅ done |
| **Phase 2 — Confidence Calibration + Live Routing** | Calibrate thresholds from agreement rate; route high-confidence traffic to the matcher | ✅ done |
| **Phase 3 — Cluster Building + Canary Promotion** | Build candidate clusters from tenant experience; canary → promote/rollback | ✅ done |
| **Phase 4 — Scale/Optimize + Verified Learning (extended scope)** | Per-tenant config, novelty detection, cost tracking (done) **+ evidence hierarchy / verification levels / two control planes (§2, §3 — not yet done, this is the corrected scope)** | 🟡 in progress — core done, verification/control-plane split is the immediate next work |
| **Phase 5 — General Language Brain Umbrella** | Formalize capability A (§1.1) as an actual orchestration layer over B–M, not just B; language-agnostic schema (§5.2) replaces the closed enum | 🟡 in progress — eval set now covers all 10 intents (130 cases); `detectedLanguage` migration audit done (no branching found); deprecation notice ✅; `app/language/glb.py` registry + per-turn record ✅ (advisory orchestration `glb_orchestrator.py` ✅ (not enacted); threshold calibration and `detectedLanguage` removal blocked on real data — see `docs/PENDING_WORK.md` |
| **Phase 6 — Own Model Learning / Distillation** (6A understanding; **6B own replies — added 2026-09-29**) | Stage 4 of §6 — a trained/distilled own model for the highest-volume patterns, with offline eval sets, model versioning, shadow/canary for the *model* (not just the cluster) | 🟡 started — dataset, model, offline eval, registry, training job built (unwired, synthetic-tested); shadow/canary/serving ⬜ — `PENDING_WORK.md` C10 |
| **Phase 7 — Continuous Learning + Adaptive Multi-Factor Routing** | Stage 5 of §6 — routing policy considers novelty/ambiguity/complexity/risk/cost/latency jointly, itself improved from measured outcomes; global vs. tenant learning (§4) formalized | ⬜ not started |
| **Phase 8 — Production Scale + Commercial Optimization** | Cost-savings KPI (§8) live in an observability dashboard; drift/degradation detection; multilingual regression benchmark suite | ⬜ not started |

### 7.2 Per-phase review (KEEP / MODIFY / MERGE / SPLIT / RENAME / ADD / REMOVE)

| Phase | Verdict | Detail |
|---|---|---|
| Phase 0 | **KEEP** | No change needed. `learning_eligible` semantics will change in Phase 4 completion (§3.1), not here — Phase 0's write path itself is correct as built. |
| Phase 1 | **RENAME** (framing only) | Keep `shadow_brain.py` exactly as built. Rename its description everywhere from "the Language Brain" to "Intent Understanding — foundation of the Own Language Brain" (§1.1). No code change. |
| Phase 2 | **KEEP** | Calibration and routing mechanics are sound and reusable by future capabilities beyond intent (e.g. a future entity-confidence calibration would reuse this same shape). |
| Phase 3 | **MODIFY** | `ClusterBuilderService` must consult `verification_level`/eligibility (§2.3) before including a row, once that column exists — currently it uses `learning_eligible` alone. This is a filtering-condition change, not a redesign. |
| Phase 4 | **SPLIT** | Split into "Phase 4 as built" (per-tenant config, novelty, cost — done, keep as-is) and "Phase 4 extended scope" (verification levels, two control planes) which is genuinely new work, tracked as the immediate next step (§9) rather than silently folded into a phase already marked done. |
| (new) Phase 5 | **ADD** | Required — nothing today formalizes capability A as an umbrella; without it, "General Language Brain" keeps meaning "the intent matcher" by default. |
| (new) Phase 6 | **ADD** | Required — Stage 4 of the model-learning path (§6) does not exist yet and is explicitly a mandatory commercial objective. |
| (new) Phase 7 | **ADD** | Required — adaptive multi-factor routing and global/tenant learning separation (§4) are both explicitly mandatory and both unbuilt. |
| (new) Phase 8 | **ADD** | Required — the cost-economics KPI (§8) is explicitly called out as a commercial requirement and has no home in the existing phase list. |

No existing phase is a **MERGE** or **REMOVE** candidate — the
repo's build-out has been incremental and non-duplicative; the
correction's own suggested Phase 5 ("Own General Language Brain") and
Phase 6 ("Own Model Learning") map directly onto this repo's history
without needing to be collapsed together.

---

## 8. Commercial cost model

### 8.1 Metrics (mostly already tracked)

`conversation_cost_tracker.py` already accumulates
`total_input_tokens`, `total_output_tokens`, and
`intent_engine_calls_saved` per conversation. This is the right
foundation. What's additionally required:

| Metric | Status |
|---|---|
| LLM calls per conversation | derivable today from `agent_run_traces` (one row/run) joined with `conversation_costs` |
| Language-understanding LLM calls specifically | not separated from other LLM calls (e.g. the final response-generation call) yet — needs a call-type tag |
| Tokens consumed | ✅ tracked (`conversation_costs`) |
| Cost per request / per conversation in **currency**, not just tokens | not done — `ROADMAP.md` §5 already flags `agent_run_traces.cost_usd` staying `NULL` for lack of a pricing table; this is the same gap |
| Own-brain-handled percentage | derivable today (`intent_engine_calls_saved / turn_count`, already the Phase 4 cost-trend query in `LANGUAGE_INTELLIGENCE.md`) |
| LLM fallback percentage | derivable (inverse of the above) |
| High-confidence local handling percentage | same as own-brain-handled, scoped to `routed_to='brain'` in `routing_decisions` |
| Cost avoided by own brain | needs the pricing table above; currently only *tokens* avoided are computed, not currency |
| Latency saved | `agent_run_traces` already has per-step latency columns (Phase 1 observability) — a brain-route vs. llm-route latency comparison query is new but needs no new column |
| Error rate / quality-regression rate | partially available via `routing_decisions.was_correct`; a broader regression-test suite (§7 Phase 8) is still needed for full coverage |

### 8.2 Baseline vs. hybrid vs. own-brain-heavy

Three cost lines the system should be able to report, once the pricing
table exists:

```
Baseline cost      = every turn priced as if it always called the LLM
                      (both Language Engine's classification-relevant
                      work and the Intent Engine call)
Hybrid cost         = actual metered cost today: LLM for Language
                      Engine (unchanged — Phase 5+ may revisit this)
                      + LLM Intent Engine calls only where routed_to='llm'
Own-Brain-heavy cost = projected cost if calibration/promotion keeps
                       expanding brain-eligible coverage at its current
                       trend rate
```

`Baseline − Hybrid = cost avoided by own brain today.` This single
number is the headline KPI ("how much money did the Own Language Brain
save?") the correction asks for. It requires only the pricing table
(§8.1) as new infrastructure — the rest is arithmetic over data already
collected.

---

## 9. Immediate next implementation step

Status update: items 1–3 below are now all implemented in code — this
section originally said "documentation only, no code changes were
made," which had gone stale (the split and the verification_level
column both shipped in a later session without this section being
updated to match). Left in priority order for reference; each item now
states what actually landed.

1. ✅ **Split the control planes (§3.1)** — done.
   `app/language/control_plane.py` is the single source of truth:
   `AUTOMATION_INELIGIBLE_INTENTS` (still `{CREATE_ORDER, ORDER_STATUS}`)
   is now independent of `LANGUAGE_LEARNING_INELIGIBLE_INTENTS`/
   `PROMOTION_INELIGIBLE_INTENTS`/`TRAINING_INELIGIBLE_INTENTS` (all
   empty — nothing is excluded from learning/promotion/training today).
   `routing_service.py` and `core_agent.py` both consult
   `control_plane.is_*_eligible()` instead of a copy-pasted frozenset.
   The not-yet-built Tool Engine is still the only place
   `automation_eligible` needs to be checked before autonomous action.
2. ✅ **Add `verification_level` (§2.2) to `language_experiences`** —
   done (`db/init/015_language_verification.sql`;
   `experience_types.py`/`experience_service.py` read/write it;
   `cluster_builder_service.py`'s `_fetch_eligible_rows()` gates on the
   §2.3 eligibility rule). One gap remained after this shipped: the
   column could reach `'self_consistent'` automatically
   (`core_agent.py`, on brain/LLM agreement) but nothing ever wrote
   `'human_confirmed'`/`'human_corrected'` — no human verification
   workflow existed. **Closed this session:**
   `app/language/verification_service.py` (`confirm_experience()` /
   `correct_experience()`) plus an admin route,
   `app/api/routes/verification.py`
   (`POST /language/experiences/{id}/confirm` and `.../correct`,
   internal-secret gated like `rag`/`memory`). `correct_experience()`
   follows §2.4 exactly: it inserts a new row with the corrected label
   rather than mutating the old one, and sets the old row's
   `superseded_by` to point at it. Tests:
   `tests/test_verification_service.py` (7 tests, fake-session style
   matching the rest of `tests/`). Still not done: nothing calls this
   automatically yet (no scheduled outcome-tracking job sets
   `outcome_positive`/`outcome_negative`, and no dashboard UI calls
   `confirm`/`correct` for a human reviewer) — this is the write path
   a future novelty-triage screen or outcome-tracking job would use,
   not that job/screen itself.
3. ✅ **Extend the Language Engine JSON schema (§5.2)** — done.
   Follow-up closed this session: §5.2's first row — the **open
   `language` tag** — had been deliberately left out of the first pass
   (`detectedLanguage` stayed the closed `bn|en|mixed|other` enum §5.1
   calls "a ceiling"). Added now, additively: `language` (open
   BCP-47-style tag; comma-separated only when genuinely code-mixed;
   `"und"` = undetermined default) on `LanguageResult` →
   `KernelRunResponse` → `LanguageExperience` → `language_experiences`
   (`db/init/017_language_tag.sql`). It is **never validated against a
   fixed set** (that is the very thing §5.1 prohibits) — only checked
   for being a non-empty string. `detectedLanguage` is unchanged and
   still populated alongside it. Human corrections
   (`verification_service.correct_experience()`) carry it over.
   `script`/`is_transliterated`/`transliterated_from`/`code_mixing`
   added to: `language_engine.py`'s prompt and parser,
   `language_types.LanguageResult`, `experience_types.LanguageExperience`,
   `experience_service.py`'s INSERT, `kernel.py`'s two KernelRunResponse
   constructions, `schemas/kernel.py`'s KernelRunResponse, and
   `core_agent.py`'s LanguageExperience build. DB: migration
   `016_language_schema_v2.sql`. Tests: 11 new tests in
   `test_language_engine.py` (script, transliteration, code-mixing,
   defensive parsing). `detectedLanguage` unchanged (additive only).
4. Everything else in §7.1's Phase 5–8 list, in that order.

   **Phase 5 start — this session:**

   Two Phase 5 prerequisites landed:

   - **`detectedLanguage` branch audit:** a full grep across
     `python-api/app/` and `node-api/src/` confirmed zero conditional
     branches on `detectedLanguage` values (`"bn"`, `"en"`, `"mixed"`,
     `"other"`). The field is only passed through — Kernel →
     `KernelRunResponse` → `core_agent.py` → `LanguageExperience` — and
     no downstream consumer (routing, shadow brain, promotion, context
     engine) makes a decision based on its value. The migration from the
     closed enum to the open `language` tag is therefore safe: no
     branching code needs updating before the deprecation notice is added.
     Immediate next step: add a `# DEPRECATED — use .language instead`
     comment to `KernelRunResponse.detectedLanguage` and
     `LanguageResult.detected_language`; schedule removal for Phase 6
     (once all tenants' `language_experiences` rows have a non-`"und"`
     `language` value from real traffic).

   - **Eval set full intent coverage:** `multilingual_intents_v1.json`
     previously covered 6 of 10 `IntentType` values (78 cases).
     `PRODUCT_INFO`, `NEGOTIATION`, `CREATE_ORDER`, and `GENERAL_QUESTION`
     were absent, which meant `PromotionService`'s generalization gate was
     silently skipped for those four intents (no cases → gate treated as
     "not applicable"). Now: 52 new cases added (4 intents × 13 language
     / script variants each), total 130 cases, all 10 intents covered.
     `test_generalization_eval.py`'s `concept_consistency` assertion updated
     from the hard-coded `5/6` (correct for 6 intents) to `(n-1)/n`
     parameterised on the actual concept count (correct for any n).
     Thresholds in `passes_gate()` remain at their placeholder values —
     calibrating them against real tenant data is the next step
     (`python -m app.language.generalization_eval --tenant N`).

   **Second pass — genuinely all-language (this session):** an audit
   found three more closed lists hiding behind "open" wording, which
   would have mis-handled any non-Bangla/English customer:
   `script` was validated against 7 values and *silently rewrote*
   Thai/Hebrew/Hangul/Cyrillic to `"latin"` (a false fact stored as
   truth); `replyLanguage` was `bn|en|other` in the prompt, so a Hindi
   customer produced `"Reply in this language: other"` in the reply
   prompt (`kernel.py`); `transliteratedFrom` examples were only
   bengali/hindi/arabic. Now: all three are open sets (only
   non-empty-string + length-cap checks — the cap matches the DB column
   so an odd value can't make the isolated INSERT fail and silently drop
   the learning row); `script` defaults to `"und"` (honest "not
   reported") instead of a guessed `"latin"`; `replyLanguage` falls back
   to the primary tag of `language` before the legacy bucket; the prompt
   now describes any language/script with multilingual examples (Hindi,
   Arabic, Spanish, Chinese, Korean, Thai, Russian, Turkish, Swahili).
   `detectedLanguage` remains a legacy field only. Pre-migration DB rows
   keep their `'latin'` default (unchanged — no backfill).

---

## 10. Terminology (use these terms consistently across all docs)

General Language Brain · Own Language Brain · LLM Teacher · LLM
Fallback · Language Experience · Verified Learning Signal · Confidence
· Calibrated Confidence · Novelty · Ambiguity · Semantic Generalization
· Continuous Learning · Own Model Learning · Adaptive Routing ·
Language Learning Eligibility · Automation Eligibility · Promotion
Eligibility · Training Eligibility

"General Language Brain" is never used as a marketing label in this
codebase's docs — every use must trace to the capability decomposition
in §1.1.

---

## 11. Answers to the 25 required questions

1. **What is the GLB?** §1.
2. **What is NOT the GLB?** §1 ("It is not...").
3. **What capabilities exist today?** §1.1 table — B, C, E, H built;
   D, F partially; G, I, J, K, L minimal/unbuilt; M built for B only.
4. **What's missing?** Same table's right column, plus §6 Stage 4+,
   §4's global layer, §8's pricing table.
5. **How will it learn from every eligible interaction?** §2.1's chain,
   gated by §2.3.
6. **How will it know if it learned correctly?** §2.2's verification
   levels + §2.4's disagreement/conflict handling.
7. **How will confidence be calibrated?** Already built — `calibration_service.py`
   (§7.1 Phase 2); extended to any future capability the same way (§7.2, Phase 2 row).
8. **How will it detect novelty?** Already built — `novelty_detector.py`;
   feeds §2.4's disagreement handling too.
9. **How will it handle ambiguity?** `is_ambiguous`/`ambiguity_reason`
   today (flag only, §5.4); resolution is future Context Engine scope
   (capability D), not language-layer scope.
10. **When will it call the LLM?** Below calibrated threshold, novel,
    high-risk, or not-yet-covered capability (per `routing_service.py`,
    extended by §7 Phase 7's multi-factor routing).
11. **When will it avoid the LLM?** Calibrated-high-confidence,
    `promotion_eligible` cluster, low business risk (§3).
12. **How will LLM calls decrease over time?** §6's five-stage
    progression; measured via §8's own-brain-handled percentage trend.
13. **How will cost savings be measured?** §8.2.
14. **How will the Own Brain improve?** §2's verified-learning loop
    feeding §7.1 Phase 3's promotion pipeline (today) and Phase 6's
    distillation (future).
15. **How will an own model eventually be trained/distilled?** §6
    Stage 4, §7.1 Phase 6.
16. **How will global multilingual capability be maintained?** §5 (schema),
    §4 (global-scope aggregation rule).
17. **How will tenant isolation be protected?** §4 table — unchanged
    from today's `tenant_id`-filtered design (`ARCHITECTURE.md` rule 1),
    extended with the explicit "never leaks into global" rule for the
    new global layer.
18. **How will customer privacy be protected?** §5.3's hard boundary
    (no identity/geography inference from language) + §4's customer-scope
    row (already isolated via `customer_memories`).
19. **How will bad learning be detected and rolled back?** §2.4,
    reusing `PromotionService`'s existing canary/rollback mechanism.
20. **How will production quality be protected?** Existing canary
    gate (`MIN_PROMOTE_ACCURACY`/`MIN_ROLLBACK_ACCURACY`, unchanged) +
    §7.1 Phase 8's regression/drift detection (future).
21. **How will the system evolve LLM-dependent → hybrid → own-brain-driven?**
    §6's five stages, §7.1's phase table.
22. **What measurable criteria determine each transition?** Existing
    numeric constants (`MIN_AGREEMENT_RATE=0.90`, `MIN_SAMPLE_COUNT=30`,
    `MIN_PROMOTE_ACCURACY=0.95`, all in `LANGUAGE_INTELLIGENCE.md`'s
    handoff table) — extended, not replaced, by §2.3's eligibility gate
    for Stage 4+ transitions once built.
23. **What is the actual commercial advantage?** §12.
24. **What should never be automatically learned?** §12's "not a moat"
    list, plus: nothing crosses from `outcome_negative`/`human_corrected`
    into a trusted signal without superseding the prior label (§2.4);
    nothing crosses tenant boundaries into the global layer except
    aggregated statistics (§4).
25. **What should remain LLM-native indefinitely?** Genuinely novel
    phrasing, high-ambiguity cases, and — per §3 — all autonomous
    business-action authorization regardless of how well language is
    understood.

---

## 12. Commercial moat — what's actually defensible

Raw customer message logs are **not** automatically a moat (they are
not defensible on their own — any competitor can also log messages).
What is defensible, and is either already built or directly planned
here:

- The **validated learning pipeline** (§2) — turning raw logs into
  trustworthy training signal is the hard, accumulating part.
- The **evaluation infrastructure** (calibration + canary + rollback,
  already built) — this is what makes it *safe* to keep improving in
  production, which is itself a capability competitors starting fresh
  don't have on day one.
- **Accumulated verified experience**, scoped correctly (tenant vs.
  global, §4) — grows more valuable and harder to replicate the longer
  the system runs.
- **Routing policy intelligence** (§7 Phase 7) — a learned policy over
  confidence/novelty/risk/cost, not a static threshold, is itself an
  asset that improves with data.
- **Specialized own models** (§6, once built) — a distilled model
  trained on this tenant base's verified experience is not reproducible
  from public data alone.
- **Integration with the actual support workflow** (Tool Engine,
  business rules, per-tenant configuration) — language understanding
  divorced from being able to act safely on it is not the product;
  the two together are.
- **Cost/performance optimization** itself (§8) — a quantified,
  continuously-measured cost advantage over pure-LLM competitors.

---

Everything above integrates with, and does not replace, the existing
`docs/LANGUAGE_INTELLIGENCE.md` implementation tracker,
`docs/LANGUAGE.md`'s field-level Language Engine reference,
`docs/ARCHITECTURE.md`'s service/data-flow reference, and
`docs/ROADMAP.md`'s prioritized task list. See each for its own detail;
this document is what keeps their terminology and phase claims
consistent with each other.
