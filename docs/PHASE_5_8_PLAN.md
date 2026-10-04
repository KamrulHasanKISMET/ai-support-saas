# docs/PHASE_5_8_PLAN.md — Phases 5–8: master handoff

**Read this first when continuing Language Intelligence work.** Every
item has an ID, a status, what blocks it, exactly how to do it, and how
to know it is finished. Deeper design: `GENERAL_LANGUAGE_BRAIN.md`. **How the own model is trained by the LLM (teacher→student), what "deep language" means and the LLM-call handover ladder: `OWN_LANGUAGE_MODEL_TRAINING_PLAN.md` (P6T-*).**
Open-item tracker: `PENDING_WORK.md`. What to collect from live traffic
and when: `REAL_TRAFFIC_DATA_COLLECTION.md`.

Last updated: 2026-09-29 (session 6: P5-4a plan persistence; earlier: P6-4 serving hook + P6R-1 knowledge ingestion).

> **Governing task spec (2026-09-29): `docs/TRAINING_GRADE_DATA_TASK.md`.
> Its required pre-coding audit (Current State Matrix, Minimal Change Plan,
> Final Gap Report), kept current every session: `docs/TRAINING_GRADE_DATA_AUDIT.md`.
> Read the audit first when resuming work — it says exactly where the
> last session stopped and what is next, so work does not proceed
> unordered.**

Status legend: ✅ done (tested with synthetic data/fakes) · 🟡 partial ·
⬜ not started · 🔒 blocked (say on what). **"Done" here never means
"proven on real traffic"** — no phase below has real-traffic evidence yet.

Rules that hold for every item (do not break them):
1. Nothing new answers a customer until it has passed offline eval →
   shadow → canary. Default is OFF / advisory.
2. No message text or person-inference field in any new table (§5.3).
3. Every threshold is a placeholder while its `THRESHOLDS_CALIBRATED` is
   False. Never lower a threshold to make something pass.
4. Never edit an old migration; add a new one (idempotent).
5. Update `PROJECT_STATUS.md` + this file in the same change.

---

## Phase 5 — General Language Brain umbrella (capability A)

| ID | Item | Status | Blocked on | Where |
|---|---|---|---|---|
| P5-1 | Capability registry A–M + drift test | ✅ | — | `glb.py`, `test_glb.py` |
| P5-2 | Per-turn `TurnUnderstanding`, persisted (90 d) + report | ✅ | apply migration 018 | `understanding_*.py` |
| P5-3 | Advisory orchestration `plan_turn()` → `metadata.plan` | ✅ | — | `glb_orchestrator.py` |
| P5-4 | Compare plans vs what really happened; decide if any rule may act | 🔒 | real traffic (needs plan persisted — see P5-4a) | — |
| P5-4a | Persist the plan (metadata only) next to `turn_understandings` | ✅ built 2026-09-29 (fakes only) | apply migration **026** (022 was already taken by model_canary) | `db/init/026_turn_plan.sql`, `understanding_store.py`, `core_agent.py`, `test_turn_plan_persistence.py` (15) |
| P5-5 | Eval cases beyond intent: typo, emoji, repeated chars, ellipsis, dialect, entity, correction (§5.4) | ✅ built (synthetic, unreviewed) | native review (A3/A9) before treating a low score as a real failure | `eval_sets/phenomena_v1.json` (22 cases, 6 layers) + `phenomena_eval.py` (18 tests). Dialect/correction layers NOT yet covered — see note below. |
| P5-6 | Triage verdicts feed learning? (C9) | ⬜ | **a human decision**, then code | `admin_novelty.py` |
| P5-7 | Calibrate generalization thresholds (A1) | 🔒 | real clusters + ≥2 runs | `calibration_apply.py` |
| P5-8 | Remove legacy `detectedLanguage` (A2) | 🔒 | real post-017 traffic, `language_tag_readiness` READY | see `PENDING_WORK.md` A2 (has the trap) |
| P5-9 | Native-speaker review of eval set (A3) | 🔒 | human reviewers | `eval_set_review.py` |
| P5-10 | Agreement-rate measurement (A4) | 🔒 | real `brain_used=TRUE` rows | `calibration_service.py` |

**P5-4a how:** add `plan JSONB` (or flat columns: `understanding_source`,
`automation`, `clarify`, `triage`) to a new table/migration 022 keyed by
the same turn; write it from `core_agent.py` where `plan` is built
(isolated, like `record_turn_understanding`). Done when a test proves
no text is stored and the plan is written with the same isolation.
**P5-4 how (once data exists):** for each turn compare `plan.understanding_source`
with what served it (`intent_source`) and `plan.clarify_before_acting`
with whether the next user turn was a correction. Decide rule-by-rule;
only then may Phase 7 let a rule act.

## Phase 6 — Own model learning / distillation

| ID | Item | Status | Blocked on | Where |
|---|---|---|---|---|
| P6-0 | Dataset builder (verified, tenant-isolated, leak-free, deterministic) | ✅ | — | `distillation_dataset.py` |
| P6-0b | Model (softmax regression + temperature), offline eval vs baseline | ✅ | — | `intent_model.py`, `model_eval.py` |
| P6-0c | Registry + lifecycle + training job | ✅ | apply migration 020 | `model_registry.py`, `train_intent_model.py` |
| P6-1 | **Train + evaluate on a real tenant** (A5) | 🔒 | ≥60 verified rows, ≥2 classes ≥12 rows each, embedding key, **and decision P6T-D1 (provider terms allow training on LLM outputs)** | `python -m app.language.train_intent_model --tenant N --dry-run` |
| P6-2 | Shadow runner (records what the model WOULD say) | ✅ built, **default OFF** | apply migration 021; set `MODEL_SHADOW_ENABLED=true`; a model in status `shadow` | `model_shadow.py` |
| P6-2a | Admin/CLI to move a model trained→shadow (with reason) and read `report_for()` | ✅ | a real DB to run against (needs Docker) | `model_admin.py` (5 tests, structural — DB dispatch is `pragma: no cover` until Docker verifies it). CLI only; no HTTP route yet, add one later if the dashboard needs it. |
| P6-2b | Purge job for `model_shadow_predictions` (`PURGE_SQL` exists) + cron line | ✅ | apply migration 021 | `model_shadow.py --purge`, `deploy/crontab.example` |
| P6-3 | **Model canary + rollback** (mirror `canary_ramp_service.py`: stages, min samples, rollback on accuracy drop, advisory lock) | ✅ built, **honesty caveat below** | apply migration 022; a model must reach `shadow` first (P6-1) | `model_canary_service.py` (14 tests). Imports `decide_ramp` from `canary_ramp_service.py` rather than re-implementing it, so both ramps share one policy. `model_admin.py start-canary` moves a model from `shadow` into this. |
| P6-4 | **Serving hook**: behind a flag, off by default, only for an `active` model, never for an intent that fails the model's per-intent floor, LLM stays the fallback | ✅ built 2026-09-29, **default OFF, never run against real Docker/DB** | apply migration 024; set `MODEL_SERVING_ENABLED=true`; an `active` model must exist (none does — P6-1 is blocked on real data) | `model_serving.py` (37 tests, fakes), `core_agent.py` (additive, flag-guarded), `db/init/024_model_serving.sql`. Decision: the hook lives in `core_agent.py` and only ADDS a hint the Kernel already validates (`predicted_intent_hint`); `routing_service.py`/`kernel.py` are untouched (a test pins that). It never overrides a turn the cluster Brain already serves. **Audit sampling:** ~10 % of served turns still get the LLM intent call, so `agrees` is real evidence (an injected hint would make `final == model` a tautology). Canary-status models serve ONLY behind a second flag `MODEL_SERVING_CANARY_ENABLED` (default off). Known limitation: it embeds the RAW message pre-Kernel while training used the NORMALIZED one — the audit sample measures that gap. |
| P6-5 | Stronger model class if softmax underperforms | 🔒 | P6-1 real result showing it does not beat the centroid baseline | keep the `IntentModel` interface |
| P6-6 | Entity/other capabilities (C) as own models | ⬜ | intent path proven first | — |

Lifecycle enforced in code: `trained → shadow → canary → active → retired`;
a model whose offline eval failed can never reach shadow; `active`
retires the previous active in the same transaction (unique index).

**⚠ P6-3 honesty caveat (read before trusting a canary result):** the
ramp built here measures **shadow agreement** with what was actually
served (LLM/brain), not real served-and-verified accuracy, because
nothing served the model when this was written (P6-4 has since been built, default OFF — see below). Shadow agreement is
upper-bounded by the served answer's own correctness and cannot see
cases where the model would have been right and the served answer was
wrong. Treat a canary that reaches "activated" under this scheme as
**"agrees with the current system a lot in shadow"**, not yet as **"is
verified correct in production"** — the two become the same claim only
after P6-4 ships and the evidence query is switched (see
`model_canary_service.py`'s module docstring, point 1, for exactly what
to change). Do not let a canary auto-activate a model into real traffic
until P6-4 is enabled AND its audited evidence is wired into this ramp; today "activated" only sets `language_models.status`,
which nothing outside this module and `model_admin.py` reads.

**P6-3 done-when, revisited:** ✅ already true for the shadow-agreement
version (ramps by stage, holds/aborts/advances per the shared policy,
per-language floor, isolated failures, advisory lock via
`scheduler_lock`, 14 tests incl. boundaries + DB failure + lock-shape
parity with `canary_ramp_service`). **Still open:** re-point the stage
evidence query at real served accuracy once P6-4 exists (one query
change, `_STAGE_ROWS`, documented in the module).
**P6-4 done when:** flag off ⇒ byte-identical behaviour (test), flag on ⇒
only an `active` model of that tenant can answer, and a
`CREATE_ORDER`/`ORDER_STATUS` answer never triggers an action
(`control_plane.is_automation_eligible`). **Status 2026-09-29: met for the
unit-testable parts** — flag defaults pinned, hook guarded by the flag
(structural test), no query/embedding when off (`test_flag_off_does_nothing_not_even_a_query`),
only active (or flagged canary) models serve, `automation_eligible` recorded from
`control_plane`. **Not yet proven:** the `core_agent.py` edit was checked by
`py_compile` + a structural test only (FastAPI/pydantic/openai are not
installable in the build sandbox) — `docker compose exec python-api python -m
unittest discover -s tests -t .` is the first real check (`PENDING_WORK.md` B1).
**P6-3 follow-up now unblocked:** re-point `model_canary_service._STAGE_ROWS`
at `model_served_turns WHERE audited AND agrees IS NOT NULL` (real
served-vs-LLM agreement) once a canary model has served audited turns. Not
done here on purpose: it changes a live ramp's evidence and needs real rows
to validate the query.

## Phase 6B — Own RESPONSE generation (capability L)  ⚠ gap found 2026-09-29

**Why this section exists.** The Phase 6 items above (P6-0…P6-6) only
make the *understanding* side own (capability B: intent). Reply
generation (capability L) is still 100 % the external LLM, and the
original design (`GENERAL_LANGUAGE_BRAIN.md` §1.1 row L, §6) marked L
"built" without ever planning an own replacement. Result: even if
everything in Phase 6 works, the LLM still writes every reply, and the
saving is only the Intent Engine call. That is not the goal. The goal
is an own model that can **answer**, with the LLM shrinking to teacher /
fallback / novel-case solver / evaluator (never zero — §11 Q25).

Design principle: **earn the right to answer, in stages, from the
safest to the freest.** Each stage reuses the same machinery as intent
(registry, shadow, canary, drift) with `capability='response'`
(`language_models.capability` already exists for this).

| ID | Item | Status | Blocked on | Notes |
|---|---|---|---|---|
| P6R-D1 | **Decision:** may reply text (which can contain names/phones/addresses) be stored for learning? Retention period, PII redaction rules, tenant consent | ⬜ | **a human/policy decision** | nothing below that stores reply text starts before this. Suggest: redact before store, ≤90 d unless approved, per-tenant opt-in |
| P6R-0 | **Response experience capture** — per turn: intent, reply_language, reply text (redacted), `reply_source` (`llm`\|`template`\|`own_model`), context ids used, and a `quality_level` mirroring verification levels: `unrated` → `customer_continued_ok` → `outcome_positive` → `human_approved` / `human_edited` (an operator's edited reply is the best label there is) / `outcome_negative` | ⬜ | P6R-D1; then a code task (migration + service + isolation like `record_language_experience`) | this is the response-side twin of `language_experiences`. **Start collecting as early as allowed: data accumulates only from the day this ships.** |
| P6R-1 | **Knowledge ingestion** (upload → clean → chunk → embed → store) so answers can be *grounded* in the tenant's real catalog/policies | ✅ built 2026-09-29 (text input), **never run on a real DB/OpenAI** | apply migration 025; a tenant must upload documents | `rag/ingestion.py`, `api/routes/knowledge.py`, `db/init/025_knowledge_ingestion.sql`, `test_ingestion.py` (29). Prerequisite for every own answer. Not done: PDF/DOCX parsing, re-embed job on model change, dashboard UI. Ingested content is tenant-authored business text, so P6R-D1 (customer reply text) does not block it. |
| P6R-2 | **Human approve/edit loop**: dashboard/API for an operator to approve, edit or reject a sent/drafted reply → writes `human_approved`/`human_edited` | ⬜ | P6R-0; a UI or admin route | the scarce resource, same as `verification_service` is for intents |
| P6R-3 | **Stage 1 — grounded approved-reply responder** (retrieval, not generation): for high-volume, low-risk intents (price, delivery info, product info), find the nearest *human-approved* reply in the tenant's library for the same intent+language, fill slots from tenant facts (price, stock, hours), answer only above a calibrated confidence. Every fact must come from tenant data, never invented | ⬜ | P6R-0, P6R-1, P6R-2 with real approved replies | the response analogue of `shadow_brain`; fully explainable, easiest to make safe. **This is the realistic first "own answer".** |
| P6R-4 | Response **shadow** (own reply computed beside the LLM's, compared, never sent) + **offline eval**: factual-consistency vs tenant data, language match (reply language = `reply_language`), forbidden-claim checks (no promises/discounts/policy the tenant did not set), human preference sample per language | ⬜ | P6R-3 built; real traffic | reuse `model_shadow.py` shape; new table `response_shadow_predictions` (metadata + reply hash, no free text unless P6R-D1 allows) |
| P6R-5 | Response **canary + rollback**, then a **serving hook** behind `routing_service`: flag OFF by default; own reply only for low-risk intents, only when `automation_eligible`-irrelevant (a *reply* is not an *action*; anything that would *act* on an order stays human/LLM-reviewed forever); LLM stays fallback; a guardrail failure ⇒ LLM answers | ⬜ | P6R-4 evidence | rollback trigger = customer re-asks / operator edits a sent reply / complaint in next turn |
| P6R-6 | **Stage 2 — own generative model** (fine-tune/distil a small model on `human_approved`/`human_edited` pairs: context+question → reply), per tenant/language group, registered as `capability='response'`, same trained→shadow→canary→active lifecycle | 🔒 | thousands of approved pairs per language; P6R-4/5 machinery proven; compute + a model-technology decision (deferred by §6 on purpose) | do NOT start earlier: a small model trained on too little data writes fluent wrong answers. Multilingual quality must pass a **per-language gate** (a language that fails is served by the LLM) |
| P6R-7 | **Response routing policy**: own responder for high-confidence + low-risk + grounded; LLM for novelty, ambiguity, complaints, negotiation, anything not grounded | 🔒 | P6R-5 real evidence; merges into P7 adaptive routing | |

**Order:** P6R-D1 (decision) → P6R-1 (knowledge ingestion, needs no
decision) and P6R-0 (capture) in parallel → P6R-2 (approve/edit loop)
→ P6R-3 → P6R-4 → P6R-5 → (much later) P6R-6/7.

**Honest expectations (do not oversell):**
- Stage 1 (P6R-3) can plausibly own a large share of repetitive replies
  once real approved replies exist. Stage 2 (own generative model) is
  research-grade work that needs real volume; whether it beats "LLM
  + retrieval" on cost/quality is unproven for this product.
- Nothing in P6R-* is built yet. The only shared pieces that exist today
  are the model registry (`capability` column), shadow/canary patterns,
  drift detectors, and pricing.
- The LLM is never removed: novel, ambiguous, complaint and any
  action-authorising turns stay LLM/human (§3, §11 Q25).

**P6R-3 done when:** flag off ⇒ identical behaviour (test); an answer
is produced only from a `human_approved` reply of the same tenant +
intent + language; every slot value is traceable to tenant data (test
with a missing fact ⇒ falls back to LLM, never invents); shadow report
shows agreement/preference numbers on real traffic.

## Phase 7 — Continuous learning + adaptive routing + global/tenant split

| ID | Item | Status | Blocked on | Notes |
|---|---|---|---|---|
| P7-1 | Routing features per turn: novelty, ambiguity, complexity, risk, cost, latency (all already recorded somewhere — inventory in `REAL_TRAFFIC_DATA_COLLECTION.md`) | ⬜ | code task (a pure feature-assembly function + tests) | do NOT change routing yet |
| P7-2 | Learned routing policy trained from outcomes | 🔒 | P7-1 features + weeks of `routing_decisions` with `was_correct` | start as a report: "what would policy X have routed differently?" |
| P7-3 | Global-vs-tenant learning layer (§4): a global table, an explicit opt-in/consent rule, tenant data never leaks | ⬜ | **a policy/legal decision** on cross-tenant learning, then schema | no global table exists today; do not train across tenants until decided |
| P7-4 | Let advisory plan rules act (from P5-4) | 🔒 | P5-4 real evidence | one rule at a time, each behind a flag |
| P7-5 | Continuous re-training schedule (retrain → offline eval → shadow, never auto-active) | 🔒 | P6-1..P6-4 proven | cron only after a human runs it manually a few times |

## Phase 8 — Production scale + commercial optimization

| ID | Item | Status | Blocked on | Where |
|---|---|---|---|---|
| P8-1 | Pricing table + honest savings estimate (no hard-coded prices) | ✅ | operator must supply prices (`MODEL_PRICING_JSON`) | `pricing.py` |
| P8-1a | Wire pricing into `conversation_costs`/trace `cost_usd` (currently NULL) and expose per-tenant savings | ⬜ | code task; needs a measured mean Intent-Engine token count | `conversation_cost_tracker.py`, `trace_service.py` |
| P8-2 | Drift/degradation detectors (PSI + agreement-drop) | ✅ | — | `drift_detection.py` |
| P8-2a | A scheduled job that runs the detectors on `turn_understandings` / `model_shadow_predictions` / `routing_decisions` and logs alerts (report only, no auto-action) | ⬜ | code task; verdicts need real windows | new `drift_job.py` |
| P8-3 | Multilingual regression benchmark suite (fixed cases run on every model/prompt change, results stored per version) | ⬜ | code task; reuse `generalization_eval` + P5-5 sets | new `regression_benchmark.py` |
| P8-4 | Cost-savings KPI in an observability view | ⬜ | frontend/dashboard or a read API first (`agent_run_traces` read API is also missing) | ROADMAP §4/§5 |
| P8-5 | Calibrate all Phase 6/8 thresholds | 🔒 | real data | flip each module's `THRESHOLDS_CALIBRATED` only via a logged run |

---

## Recommended order for the next session

0. **Decide P6R-D1**, then start **P6R-1** (knowledge ingestion) and **P6R-0** (reply capture): these have the longest lead time because their data only exists from the day they ship. See *Phase 6B*.
1. **P6-2a, P6-2b** (small; makes shadow usable and purgeable).
2. ~~P6-3 model canary, P6-4 serving hook~~ ✅ built (P6-4 2026-09-29, flag OFF). Next for this item: re-point the canary evidence (see P6-4 note).
3. ~~**P5-4a** persist the plan~~ ✅; ~~**P5-5** phenomena eval cases~~ ✅.
4. **P8-1a, P8-2a, P8-3**.
5. **P7-1** feature assembly (pure). Then stop: everything left is 🔒
   until real traffic and the human decisions (P5-6, P7-3) exist.

## What is deliberately NOT done and why
- **Own reply generation (Phase 6B) is not built.** It was missing from the original design; it is now specified above.
- Nothing here has run against a real DB/embedding provider (see
  `PENDING_WORK.md` B1) — the Docker suite and migrations 018–021 are the
  first real check.
- No model is trained: there is no verified data yet.
- No threshold is calibrated; each carries `THRESHOLDS_CALIBRATED = False`.
