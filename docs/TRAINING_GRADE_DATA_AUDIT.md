# docs/TRAINING_GRADE_DATA_AUDIT.md — Current State Matrix, Minimal Change Plan, Gap Report

Companion to `docs/TRAINING_GRADE_DATA_TASK.md` (the governing spec).
This file is the required pre-coding audit, kept up to date every
session so the *next* session can resume from here instead of
re-discovering the codebase. **Read this file first, before
`PHASE_5_8_PLAN.md`, when picking up Language Intelligence work.**

Last updated: 2026-09-29 (audit session). Evidence checked directly
against source files in this codebase, not against other docs' claims
— several rows below correct an over-statement made in an earlier
document (noted inline).

---

## 1. Current State Matrix

Legend: **Existing** (built, wired into a real path) · **Partial**
(built but incomplete, or built but not wired) · **Missing** (not
built).

### 9 language capability layers (evaluation dimension, not services)

| Layer | Status | Evidence |
|---|---|---|
| L1 Surface variation | Partial | LLM handles it implicitly (no code enforces it); measured by `eval_sets/phenomena_v1.json` L1 cases (6) + `generalization_eval` for cross-script. No own-model coverage yet (own model doesn't exist in production). |
| L2 Script & transliteration | Existing (evaluation) / Missing (own model) | `language_engine.py` captures `script`, `is_transliterated`; `generalization_eval.py` tests it (13 variants, `multilingual_intents_v1.json`). |
| L3 Cross-language equivalence | Existing (evaluation) / Missing (own model) | Same file, same test; `intent_model.py` is trained/evaluated per-language (`model_eval.py` per_language slices) but never trained on real data yet. |
| L4 Code-mixing | Partial | `code_mixing` field captured (`language_engine.py`); 4 cases in `phenomena_v1.json`; no dedicated eval slice/gate beyond that. |
| L5 Context-dependent meaning | Missing | **Correction to earlier doc:** `OWN_LANGUAGE_MODEL_TRAINING_PLAN.md`'s table already says this; confirmed here by re-checking `language_engine.py` and `experience_service.py` — neither captures prior-turn context alongside a Language Engine call. `phenomena_v1.json` has 4 cases but nothing runs against them yet (no model exists). |
| L6 Implied goal | Partial | 4 eval cases exist (`phenomena_v1.json`); the LLM prompt is not audited here for whether it explicitly optimizes for this (out of scope — would touch `LANGUAGE_UNDERSTANDING_PROMPT`, not touched this session per "preserve, minimal extension"). |
| L7 Negation/conditions | Partial | 3 minimal pairs in `phenomena_v1.json`, with pair-consistency scoring in `phenomena_eval.py`. This is the layer this session's dedup work (§ below) was explicitly designed around (see `experience_shape.py` docstring). |
| L8 Entities & references | **Partial, coarser than documented elsewhere** | `entity_spans` (`language_engine.py`) is a **flat list of raw substrings**, not typed (`{type, value}`). `experience_shape.py` (this session) works around this honestly (untyped `<e>` placeholder) but typed entities (needed for real L8 training, P6T-4) do not exist. This is a correction: earlier sessions referred to "typed entities" as a near-term item; it requires an LLM-prompt change not made yet. |
| L9 Response capability | Missing | 100% LLM; no own response path (Phase 6B / P6R-* in `PHASE_5_8_PLAN.md`, all ⬜). |

### Core pipeline / storage

| Item | Status | Evidence |
|---|---|---|
| Raw message preserved, never overwritten | **Existing** | `language_experiences.original_message` stored as-is; `normalized_message` is a separate derived column (`db/init/010_language_experience.sql`). Matches the task's "Raw Data Preservation" requirement already, with no change needed. |
| `normalized_message` ≠ full semantic representation (conceptual boundary) | Partial | The column exists and is treated as one signal among several (intent, entities, language are separate columns) — the codebase does not conflate them structurally. No explicit "semantic representation ≠ normalized text" enforcement exists because nothing currently computes a language-independent meaning representation distinct from normalized text; this is conceptually correct today only because that richer representation doesn't exist yet to be confused with it. |
| Semantic/entity-normalized shape (for dedup, NOT for meaning) | **Existing (built this session)** | `experience_shape.py` — pure, tested (19 tests). Explicitly scoped to dedup only, not claimed as "meaning". |
| Deduplication (exact) | Missing at storage layer; Existing at training layer | `distillation_dataset.py` already case/whitespace-dedupes at TRAINING time (§3 of that module). `language_experiences` itself still writes one row per turn, unchanged — this session did NOT touch that write path (see Minimal Change Plan §2, explicit risk call). |
| Bounded/reservoir retention for near-duplicates | **Partial — designed, schema exists, NOT wired** | Design: `EXPERIENCE_DEDUPLICATION_AND_RETENTION.md`. Schema: `db/init/023_experience_groups.sql` (empty by construction until P6D-6). Measurement tool to size it: `experience_backfill_report.py` (read-only, 10 tests). |
| Retention policy (time-based) for `language_experiences` | **Missing** (unchanged from before) | Documented gap in the migration's own comment; still an open owner decision (P6D-2). |
| Tenant isolation on every learning record | **Existing** | Every relevant table (`language_experiences`, `turn_understandings`, `model_shadow_predictions`, `language_models`, `experience_groups`) has `tenant_id` and every query in the reviewed modules filters on it; `distillation_dataset.py` actively **raises** on a cross-tenant row rather than silently dropping it (tested). |
| Global vs tenant-specific learning separation | **Missing** | No global table exists; `PHASE_5_8_PLAN.md` P7-3 already flags this as blocked on a policy decision. Confirmed still true. |

### Teacher data / response learning signals

| Signal | Status | Evidence |
|---|---|---|
| Teacher reply text | Missing | Not stored anywhere; blocked on owner decision P6R-D1 (may reply text be stored). |
| Reply language | Existing (per-turn) | `language_experiences.reply_language`. Not yet joined to an actual stored reply (since the reply itself isn't stored). |
| Knowledge/RAG references used | Missing | No column/table records which RAG chunks (if any) informed a given reply. RAG itself exists (`app/rag/` per ROADMAP) but its retrieval-time IDs are not persisted per-turn. |
| Context/state used | Missing | Confirmed above (L5 row) and in `OWN_LANGUAGE_MODEL_TRAINING_PLAN.md` §3 — unchanged. |
| Teacher uncertainty/confidence | Partial | `llm_raw_confidence` exists but is explicitly documented as **uncalibrated, self-reported** (`010_language_experience.sql` comment) — not the same claim as "teacher was asked twice and agreed" (P6T-1, still ⬜). |
| Teacher model/version | Existing | `llm_model_version` column, populated. |
| Verification/quality level | **Existing** | `verification_service.py`, levels `unverified → self_consistent → outcome_positive/human_confirmed`, already the backbone of `distillation_dataset.py`'s eligibility gate. This satisfies the task's "Quality Levels" requirement — no new concept needed, confirmed by direct inspection. |
| Outcome signal | Partial | `outcome_positive` verification level exists as a *label category*; what business event promotes a row to it was not re-audited this session (out of scope, no evidence gathered either way — flagged rather than guessed). |

### Dataset construction / evaluation / own-model readiness

| Item | Status | Evidence |
|---|---|---|
| Dataset Builder separate from runtime | **Existing** | `distillation_dataset.py` runs offline (`train_intent_model.py`), never in the request path; a structural test in `test_train_intent_model.py` asserts core_agent/kernel/routing never import training modules. |
| Filtering by quality/verification/language/tenant | **Existing** | Same module: verification-level SQL gate, per-tenant fetch, leakage guard against the eval set, deterministic split. |
| Filtering by semantic cluster/difficulty/diversity | **Missing** | Not implemented; would need the shape/group concept (§ above) to mature past P6D-6 first. Correctly listed as a future filter, not attempted this session. |
| Evaluation beyond intent accuracy | **Partial** | `generalization_eval.py` (cross-language/script) + `phenomena_eval.py` (this session's predecessor: L1,L4-L8) + `model_eval.py` (accuracy/F1/ECE/coverage/per-language vs baseline). Response-quality evaluation (L9) does not exist (no own response path to evaluate). |
| Own-model training/handover plan | **Existing (as a plan)** | `OWN_LANGUAGE_MODEL_TRAINING_PLAN.md`, R0-R6 ladder. Actual own model: trained-nothing (blocked on real verified data, P6-1). |
| Shadow / canary infrastructure | **Existing (unproven on real data)** | `model_shadow.py`, `model_canary_service.py`, both tested with fakes; explicitly documented as measuring shadow *agreement*, not real served accuracy, until a serving hook exists. |

---

## 2. Minimal Change Plan (this session's scope)

Only items where real evidence showed a genuine, addressable gap were
touched. Everything else in §1 marked Partial/Missing is intentionally
**not** touched this session — see §3.

### Change 1 — Entity-normalized shape function

- **What:** `experience_shape.py`: `canonical_form`, `shape_hash`, `group_key`.
- **Why:** Required before any near-duplicate bounding can be measured
  or built (task's "Data Quality & Diversity" + prior session's dedup
  design). Building the measurement tool first, before touching the
  write path, follows the task's own `AUDIT → ... → MINIMAL EXTENSION`
  order.
- **Where:** New file, `app/language/`. No existing file modified.
- **How:** Pure function, entity spans masked with one untyped
  placeholder (honest about today's untyped `entity_spans`), tenant +
  intent + language always required alongside the shape (never shape
  alone) so a minimal pair (different confirmed intent) can never
  collapse.
- **Risk:** Very low — pure, unused by any runtime path, exercised only
  by tests and the read-only report below.

### Change 2 — Duplication measurement report (P6D-8)

- **What:** `experience_backfill_report.py`.
- **Why:** The task and the dedup design both require **measuring
  before deciding** the reservoir cap (P6D-1) and retention period
  (P6D-2) — this is that measurement, safe to run against real data any
  time because it only reads.
- **Where:** New file. `run_for_tenant`/`fetch_rows` are DB-only
  (`pragma: no cover` here, same convention as every other DB-touching
  module this project already uses); `summarize_groups` is pure and has
  10 tests.
- **How:** Groups existing `language_experiences` rows by
  `experience_shape.group_key`, reports distinct-group count, a size
  histogram, the largest groups, and how many rows a candidate cap M
  would have saved — so the owner can pick M from evidence.
- **Risk:** Low — read-only SQL, no schema change required to run it
  (it reads the existing table only).

### Change 3 — `experience_groups` schema (P6D-4), NOT wired

- **What:** `db/init/023_experience_groups.sql`.
- **Why:** Reviewable now; nothing can write to it until P6D-1 (cap
  size) is decided from real §Change-2 output, so building the write
  path first would be guessing a schema before the evidence exists —
  the task explicitly warns against exactly that pattern ("Do not add
  all of these blindly").
- **Where:** New migration only. `scripts/migrate.sh` updated to include it.
- **How:** Migration only; the table is expected to stay **empty** until
  a future session builds the write path (P6D-6) after P6D-1 is decided.
- **Risk:** None to running systems (new table, nothing reads/writes
  it); the only risk is someone mistaking its existence for the feature
  being live — stated explicitly in the migration's own header comment
  and in the Current State Matrix above (Partial, not Existing).

### Change 4 — This audit + task-spec documents

- **What:** This file and `TRAINING_GRADE_DATA_TASK.md`.
- **Why:** The task requires the audit as a deliverable *before* coding,
  and requires a session-independent record so work is not "random"
  across sessions (the person's explicit instruction this turn).
- **Risk:** None (documentation).

## 3. Explicitly NOT done this session, and why (per the task's own instruction to name these)

- **No write-path change to `language_experiences` or
  `experience_service.py`.** The dedup/reservoir write path (P6D-6) is
  the highest-risk item in this whole area — it touches a path already
  live in production-shaped code, wrapped in isolation, called from
  `core_agent.py`. Changing it without Docker/real-DB verification
  available in this session would violate "preserve all existing
  working behavior." It is next in the ordered plan (§4), *after*
  Change 2's report has real numbers to size it correctly, per the
  task's own audit-before-extension instruction.
- **No typed entity extraction (L8).** Would require an LLM prompt
  change (`LANGUAGE_UNDERSTANDING_PROMPT`) and a new response schema —
  a real architecture touch-point, not a minimal extension. Documented
  as Partial with the honest reason (P6T-4, already tracked).
- **No teacher-reply capture (P6R-0), no context capture (P6T-2), no
  RAG-reference capture.** All three are blocked on the owner decision
  P6R-D1 (may reply/context text be stored) — building the storage
  before that decision would pre-empt it. Confirmed still blocked, not
  re-attempted.
- **No response-generation work (L9, R4/R5).** The task explicitly
  forbids this for the current pass ("Do NOT build a separate
  response-generation architecture yet... Do not implement R5
  prematurely").
- **No semantic-cluster/difficulty dataset filtering.** Depends on the
  group concept from Change 3 maturing past the write-path stage first.
- **No new microservice, no new database, no new ML framework, no Redis
  durable use, no Core Agent rewrite.** Verified by re-reading
  `core_agent.py`, `kernel.py`, `routing_service.py` — none were
  modified this session (only additive hooks from earlier sessions
  remain, unchanged).

## 4. Ordered next steps (resume here)

1. **Run `experience_backfill_report.py` against real tenant data**
   (needs Docker + real traffic — first blocking step, same class as
   every other 🔒 item in `REAL_TRAFFIC_DATA_COLLECTION.md`).
2. **Owner decides P6D-1 (cap M) and P6D-2 (retention N)** using that
   report's histogram/`rows_saved_at_cap` output — not a guess.
3. **Build P6D-6**: the reservoir-sampling write path, as an additive,
   isolated call alongside the existing `record_language_experience`
   (same isolation pattern as `record_turn_understanding` /
   `model_shadow.run_shadow` — never blocks the reply on failure).
4. **Build P6D-7**: time-based purge of raw-text samples only, exempting
   `human_confirmed`/`human_corrected` rows.
5. Everything else in `PHASE_5_8_PLAN.md`'s "Recommended order for the
   next session" continues to apply unchanged (P6-4 serving hook,
   P6R-1 knowledge ingestion, etc.) — this audit does not reorder those,
   it only inserts the P6D-8 → decide → P6D-6 → P6D-7 sequence ahead of
   P6D-6/7 specifically, since those were previously listed without a
   measurement step in front of them (a gap this audit fixes).

## 5. Final Gap Report (per the task's required format)

**IMPLEMENTED** (this session, tested, not wired to any live write path):
- `app/language/experience_shape.py` (19 tests)
- `app/language/experience_backfill_report.py` (10 tests, DB-read-only)
- `db/init/023_experience_groups.sql` (schema; table stays empty by design)

**PARTIAL** (existed before this session, re-confirmed by direct evidence,
corrected where an earlier document overstated status):
- Entity capture (untyped only, not typed — corrected)
- L1, L4, L6, L7 evaluation (small eval sets exist, no model to run them
  against yet)
- Teacher uncertainty (raw confidence exists, calibrated/repeated-sample
  uncertainty does not)
- Dataset builder filtering (quality/verification/language/tenant yes;
  semantic-cluster/difficulty no)

**DOCUMENTED ONLY** (a plan exists, zero code):
- Own response generation (Phase 6B / P6R-*)
- Context/RAG-reference capture (P6T-2)
- Teacher-reply capture (P6R-0)
- Global-vs-tenant learning split (P7-3)
- Reservoir write path itself (P6D-6) and its purge (P6D-7) — schema
  exists (this session), write path does not

**FUTURE** (explicitly out of scope, not started, not designed in detail):
- Typed entity extraction (L8 real version)
- Semantic-cluster/difficulty-based dataset filtering
- Response-quality evaluation (L9)
- Any own generative model (R5)

## 6. Session 5 addendum (2026-09-29): P6-4 serving hook

- **Added:** `app/language/model_serving.py`, `db/init/024_model_serving.sql`,
  two default-OFF flags, an additive flag-guarded hook in `core_agent.py`,
  `tests/test_model_serving.py` (37). Training-grade relevance: serving writes
  **metadata only**, and `agrees` is set only on audited turns (LLM still
  classifies them), so served turns never manufacture fake "verified"
  evidence for later training.
- **Not touched:** `kernel.py`, `routing_service.py`, `language_experiences`
  write path, P6D-6/P6D-7 (still waiting on the P6D-8 real-data report and
  owner decisions P6D-1/P6D-2), P6R-* (still blocked on P6R-D1).
- **Verification level:** unit tests with fakes pass (457 tests run; the same 17
  modules that need `openai`/`anthropic`/`pydantic` fail to import in the
  sandbox both before and after — identical failure list). The `core_agent.py`
  edit has NOT been executed; run the Docker suite first.

### Session 5, second change (2026-09-29): P6R-1 knowledge ingestion

- **Added:** `app/rag/ingestion.py`, `app/api/routes/knowledge.py`, migration `025`,
  `tests/test_ingestion.py` (29). Registered in `main.py`; `kernel.py` and
  `context_engine.py` untouched (a test pins that).
- **Why now:** the task's order puts P6R-1 in parallel with P6R-0 and it needs no
  owner decision — it stores tenant-authored business text, not customer messages
  or replies. P6R-0 / P6R-2 / P6R-3+ remain blocked on P6R-D1.
- **Verification:** same sandbox limits as above; the identical 17 pre-existing
  import failures (missing `openai`/`anthropic`/`pydantic`) before and after.
  Not run against real Postgres/OpenAI.

## 7. Session 6 addendum (2026-09-29): P5-4a plan persistence

- **Added:** `db/init/026_turn_plan.sql` (6 nullable columns on `turn_understandings`),
  `plan=` argument + `plan_to_params()` in `understanding_store.py`, one-line
  pass-through in `core_agent.py`, `tests/test_turn_plan_persistence.py` (15).
- **Training-grade relevance:** records *how the system decided to treat each turn*
  (teacher required / human review / triage) as metadata, so later sessions can
  measure plan-vs-outcome instead of guessing. No text, no person-inference field.
- **Not touched:** `language_experiences` write path, P6D-6/P6D-7 (still waiting on the
  P6D-8 real-data report + owner decisions), P6R-* (blocked on P6R-D1), routing/kernel.
- **Verification level:** unit tests with fakes: 504 tests ran, the 16 import failures are
  all missing `sqlalchemy/openai/anthropic/fastapi` in the sandbox (pre-existing).
  `core_agent.py` edit: `py_compile` + structural test only. Not run on real Postgres.
