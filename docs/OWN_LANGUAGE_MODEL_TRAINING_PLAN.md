# docs/OWN_LANGUAGE_MODEL_TRAINING_PLAN.md — training the own language model from the LLM (teacher → student)

Purpose: one place that says **how the own language model is trained by
the LLM now, what it must learn (deep language, not surface strings),
and when — by measurable criteria — LLM calls may be reduced.**
Task IDs here are `P6T-*`. They extend `PHASE_5_8_PLAN.md` (Phase 6/6B);
architecture background is `GENERAL_LANGUAGE_BRAIN.md`; live-traffic
checkpoints are in `REAL_TRAFFIC_DATA_COLLECTION.md`. **How raw experiences
are deduplicated/retained without losing minimal-pair signal (directly
affects what the dataset builder in §3/§4 pulls from):
`docs/EXPERIENCE_DEDUPLICATION_AND_RETENTION.md`.**

Last updated: 2026-09-29. Status legend as in `PHASE_5_8_PLAN.md`.

---

## 1. The principle (product owner's direction, recorded)

1. The external LLM understands language at **depth** — meaning, intent,
   context, implication — not surface wording. It also produces the
   reply. That capability is the **teacher**.
2. The own model must learn that **depth** from the teacher. A model that
   only memorises phrases ("price koto?" → PRICE_INQUIRY) is not the
   goal (`GENERAL_LANGUAGE_BRAIN.md` §5.5 "phrase memorization vs
   semantic abstraction").
3. **Now (teaching period):** the LLM keeps doing all the language work.
   Every LLM call is also a training example. Nothing is reduced early.
4. **Later (handover):** LLM calls are reduced **only when the own model
   has measurably reached the bar, capability by capability** (§5). Until
   then we wait — reducing calls early trades customer quality for cost.
5. The LLM never disappears: teacher, fallback, novel-case solver,
   evaluator (§11 Q25 of the GLB doc).

## 2. What "deep language" means here — and how we will know the student has it

Surface-level matching is what the current nearest-example matcher and
the softmax model can degrade into. Each row is a depth layer, the
teacher signal that carries it, and the **test that proves the student
learned meaning rather than strings**.

| Layer | Example | Teacher signal | Proof test | Test exists? |
|---|---|---|---|---|
| L1 Surface variety | typos, spelling, slang, repeated letters, emoji | normalized text | same goal under corrupted forms scores the same | ⬜ (P5-5) |
| L2 Script / transliteration | `দাম কত?` vs `dam koto?` | `script`, `is_transliterated` | cross-script concept consistency | ✅ `generalization_eval` (13 variants) |
| L3 Cross-language equivalence | `how much?` / `कितना?` / `¿cuánto?` | shared goal label | per-language accuracy slices + concept consistency | ✅ `generalization_eval` |
| L4 Code-mixing | `ei product ta available ache?` | `code_mixing` | mixed-language turns as their own slice | 🟡 flag recorded, no eval slice yet |
| L5 Context-dependence | "ar ta?" ("and that one?") means different things by prior turn | intent **given** context | same string, two contexts → two labels | ⬜ (P5-5; needs context captured, P6T-2) |
| L6 Pragmatics / implied goal | "it arrived broken" = complaint + wants replacement, no question mark | goal, not literal form | held-out implied-goal cases | ⬜ (P5-5) |
| L7 Negation / conditions / quantity | "I do NOT want it", "only if red" | intent + entities | minimal-pair tests (flip one word) | ⬜ (P5-5) |
| L8 Entities & references | product, order id, quantity, "the second one" | typed entities, entity_spans | entity-level accuracy | ⬜ (P6T-4) |
| L9 Appropriate reply | right content, tone, language | reply + rationale | human preference + factual check (P6R-4) | ⬜ |

Rule: **no capability may take over from the LLM until its layers above
are measured on held-out data, per language, and pass.** The existing
per-language slice gates (`model_eval`, `generalization_eval`,
`model_shadow`) already enforce the "one bad language blocks" principle;
new layers add new slices in the same way.

## 3. What the teacher gives us today, and what is missing

The Kernel makes up to **four** LLM calls per turn (language
understanding, intent classification, reply generation, memory
extraction — `kernel.py`). Each is a separate teaching channel.

| Teacher output | Captured today? | Where | Gap |
|---|---|---|---|
| Language: normalized message, script, transliteration, code-mixing, ambiguity, entity spans, reply language, style | ✅ | `language_experiences` | — |
| Intent + entities + confidence + source | ✅ (intent); 🟡 entities not stored as labelled training pairs | `language_experiences.final_intent`, trace | store typed entities as labels (P6T-4) |
| Model version of the teacher | ✅ | `llm_model_version` | keep; a teacher upgrade changes label style |
| **Conversation context used** (prior turns, state) | ⬜ | not stored | needed for L5 — P6T-2 |
| **Knowledge/facts the teacher used** (RAG chunks, tenant rules) | ⬜ | not stored | needed for grounded replies — P6R-1/P6T-2 |
| **Reply text + quality label** | ⬜ | not stored | Phase 6B P6R-0 (needs decision P6R-D1) |
| **Teacher uncertainty** (agreement across repeated samples) | ⬜ | not stored | P6T-1: a label the teacher is unsure of should not teach |
| Memory-extraction outputs | 🟡 written to `customer_memories`, not as training pairs | | later (P6T-6) |

## 4. Trust: the teacher's label is a *candidate*, not the truth

This does not contradict "train from the LLM". It defines **how much a
teacher label may teach**, using the evidence hierarchy already built
(`GLB` §2, `verification_level`):

- Teacher label alone → `unverified`: usable for **shadow comparison and
  as low-weight pre-training**, never as evaluation truth.
- Teacher + own model agree, above the tenant's reliability → `self_consistent`
  (already automatic) → eligible for training (today's gate).
- Outcome-confirmed or human-confirmed → `outcome_positive` /
  `human_confirmed` → highest weight, and the **only** labels used to
  *evaluate* the student.
- Human-corrected → supersedes the teacher's label (never deleted).

Consequences to state plainly:
- **The student cannot exceed the teacher** on what it is trained from,
  and it inherits the teacher's mistakes unless verification catches
  them. Human-verified evaluation data is what tells us the truth.
- Evaluation sets must stay separate from training data (leakage guard
  exists: `distillation_dataset`).

## 5. The handover ladder — reduce LLM calls one channel at a time

Ordered from lowest risk / most data-rich to highest. Each rung is a
separate gate; a rung is entered only when its gate passes on **real
data**, never on synthetic tests. All gates use the existing lifecycle:
offline eval → shadow → canary → active, with the LLM as fallback.

| Rung | LLM call replaced | Own component | Status | Gate (starting points — uncalibrated) |
|---|---|---|---|---|
| R0 | none | LLM does everything; every call captured | ✅ running (Phase 0) | — |
| R1 | **Intent Engine** for high-confidence turns | cluster matcher (live routing, Phase 2–3) → trained intent model (Phase 6A) | 🟡 matcher live; model built, unwired | agreement ≥ 0.90 shadow, answered-agreement ≥ 0.95, no failing language slice, then canary |
| R2 | **Language-understanding call** (normalization, script, transliteration, code-mixing, entity spans) | own normalizer/detector (script + language ID + rules + small model) | ⬜ not designed in detail | per-field accuracy vs teacher-and-human sample, per language; L1/L2/L4 tests |
| R3 | **Memory extraction** call | own extractor from entities | ⬜ | precision on extracted facts vs human sample; §5.3 no-inference boundary must hold |
| R4 | **Reply** for repetitive low-risk intents | approved-reply retrieval responder (P6R-3) | ⬜ | factual consistency 100 % on grounded slots, human preference ≥ LLM-parity on sample, per language |
| R5 | **Reply** more broadly | own generative model (P6R-6) | 🔒 | needs thousands of approved pairs per language + proven R4 machinery |
| R6 | routing across all of the above | adaptive routing policy (Phase 7) | 🔒 | learned from measured outcomes |

**KPI (already recorded, nothing new to build to read it):**
LLM calls per turn and tokens per resolved conversation
(`conversation_costs.intent_engine_calls_saved`, `agent_run_traces`
tokens). Extend `intent_engine_calls_saved` to one counter per rung
when R2+ exist (P8-1a).

**The waiting period is explicit:** between R0 and each later rung, the
only thing that must happen is **collect richer teacher data and
verified labels**. The next rung is not started early to "save cost".

## 6. Tasks

| ID | Task | Status | Blocked on |
|---|---|---|---|
| P6T-0 | This plan + link from the other docs | ✅ | — |
| P6T-1 | **Teacher-uncertainty capture**: for a sampled % of turns, run the teacher 2–3× (or with a paraphrase) and store only agreement + label (no extra text); low-agreement labels are excluded from training and routed to human review | ⬜ | code task; extra LLM cost → needs a sample-rate setting, default 0 |
| P6T-2 | **Context & knowledge capture as training inputs**: store the ids/hashes and structured state (not free text) of what the teacher saw — prior-turn ids, state slots, RAG chunk ids, rules applied — so a context-aware student (L5) and grounded replies can be trained later | ⬜ | code task; text-bearing parts wait for P6R-D1 |
| P6T-3 | **Deep-language eval sets** (L1, L4–L7): typos/emoji/slang, code-mixed, context-dependent pairs, implied-goal, negation minimal pairs, in the same JSON shape as `multilingual_intents_v1.json` with a per-layer field and per-layer gate | ⬜ | code task; native review later (A3); this is P5-5 |
| P6T-4 | **Entity training pairs**: persist typed entities from the Intent Engine as labels; add an entity model + entity-level eval (capability C) | ⬜ | code task; shares its entity-normalization function with dedup's shape hash, `EXPERIENCE_DEDUPLICATION_AND_RETENTION.md` P6D-5 |
| P6T-5 | **Rung R2 design + build**: own language-understanding component and its offline eval against teacher + human sample | ⬜ | R1 evidence first; design task can start now |
| P6T-6 | Memory-extraction pairs (rung R3) | ⬜ | later |
| P6T-7 | **Per-rung KPI**: one counter per handover rung in `conversation_costs`, exposed in a report | ⬜ | code task (with P8-1a) |
| P6T-8 | **Teacher-version drift**: when `llm_model_version` changes, flag it, compare new-teacher labels to old on a fixed set before mixing into training | ⬜ | code task; uses `drift_detection` |
| P6T-D1 | **Decision — provider terms:** confirm that the LLM provider's terms allow using its outputs to train an own model that reduces use of that provider. Many providers restrict using outputs to build competing models. **Check the current terms and, if unclear, get written confirmation, BEFORE any distillation of LLM outputs into a trained student** (P6-1 onward) | ⬜ | **a legal/contract check by the product owner — not something the code can settle** |
| P6T-D2 | **Decision — teaching budget:** how much extra LLM spend is acceptable for P6T-1 (repeat sampling) during the teaching period | ⬜ | owner |
| P6R-D1 | (from `PHASE_5_8_PLAN.md`) may reply text be stored for learning | ⬜ | owner |

## 7. Risks, stated plainly
- **Provider terms (P6T-D1)** may forbid the core plan. Resolve first;
  the alternative (own labels from humans/outcomes, or a different
  teacher whose terms permit it) changes the schedule, not the design.
- **Inherited errors:** a student trained on teacher output repeats its
  blind spots. Mitigation: verification levels (§4), human evaluation
  sets, per-language gates.
- **Depth is not automatic:** a small model on embeddings will be strong
  on L1–L3 and weak on L5–L7 (context, implication, negation) unless it
  is given context and tested there. Those layers may need a stronger
  model class (P6-5) or may permanently stay with the LLM.
- **Distribution shift:** new products, seasons, slang. `drift_detection`
  reports; retraining is a human-triggered job until proven.
- **Privacy:** training text is customer text. Tenant-isolated only
  (§4 of GLB doc); cross-tenant learning is blocked on P7-3.
- **Cost of teaching:** P6T-1 and shadow scoring add calls; they are
  flags, default off.

## 8. Order of work (next sessions)
1. Owner: **P6T-D1** (provider terms), **P6R-D1** (reply storage), **P6T-D2** (budget).
2. Code, no decision needed: **P6R-1** knowledge ingestion, **P6T-3/P5-5** deep-language eval sets, **P6-2a/2b, P6-3/4** (shadow admin, canary, serving hook), **P6T-7/P8-1a** per-rung KPI.
3. Code after decisions: **P6T-1**, **P6T-2**, **P6R-0**.
4. Real traffic: run the checkpoints in `REAL_TRAFFIC_DATA_COLLECTION.md`;
   enter rung R1's canary only when its gate passes; then design/build R2.
