# docs/LANGUAGE_INTELLIGENCE.md

**Architecture and terminology now live in
`docs/GENERAL_LANGUAGE_BRAIN.md` — read that file first.** This file
remains the repo-side *implementation tracker* (what's built, which
test file to run, which SQL query to check progress) for the Phase 0–4
work described there. Where anything below uses older terminology or
framing ("the Language Brain" meaning the intent matcher alone, a
single `learning_eligible` flag, etc.), `GENERAL_LANGUAGE_BRAIN.md`'s
corrected terminology and the two-control-plane split (its §3) are the
current source of truth — this file's phase statuses and file/test
references are still accurate and unchanged.

Tracks the evolution from "100% LLM does language understanding"
toward "own Language Brain + LLM as teacher/fallback" (the Language
Intelligence design). **Read this file before continuing this specific
line of work** — it says exactly what phase the repo is in right now
and what the next concrete step is, so work can resume cleanly in a
new session without re-deriving the plan.

> **Terminology correction:** everywhere below that says "the Language
> Brain" or "own Brain" refers specifically to **Intent Understanding**
> (capability B in `GENERAL_LANGUAGE_BRAIN.md` §1.1) — the embedding
> nearest-example matcher (`shadow_brain.py`) — which is the
> **foundation of** the General Language Brain, not the finished thing.
> The umbrella General Language Brain (capability A) is not built yet;
> see that doc's §7 for the phases (5–8) that build it.

## Where this came from

A full architecture design (language-agnostic principles, confidence
calibration, adaptive routing, learning/promotion pipeline, multi-tenant
experience isolation, 5-phase rollout) was produced first, separately
from this repo, then implementation started here following its Phase
0 → Phase 4 plan. This file is the repo-side tracker for that plan —
it does not restate the full design, only what applies to this
codebase and what's actually been built.

## Current phase: **Phase 4 — Scale/Optimize (in progress)**

Phases 0–3 are done. Phase 4 (Scale/Optimize) is in progress:
per-tenant calibration config, novelty detection, and cost tracking
are built and wired. See Phase 4 section below for what is done
and what is next.

## Phase 0 — Foundation (done)

Goal: nothing about language *understanding* changes yet (still 100%
LLM, via the existing `app/language/language_engine.py`, unchanged).
What Phase 0 adds is **logging**, so nothing observed from here on is
lost while later phases build on top of it.

**What was built:**

| Piece | File |
|---|---|
| `language_experiences` table | `db/init/010_language_experience.sql` |
| Row type | `app/language/experience_types.py` (`LanguageExperience`) |
| Write path (isolated, non-blocking) | `app/language/experience_service.py` (`record_language_experience`) |
| Wiring | `app/agent/core_agent.py` — called right after `record_trace()`, same isolation pattern |
| New (additive) fields on `KernelRunResponse` | `communicationStyle`, `isAmbiguous`, `ambiguityReason`, `entitySpans`, `languageConfidence` — previously computed but only logged inside `kernel.py`; now also returned so `CoreAgent` doesn't need to re-derive them |
| Tests | `tests/test_language_experience.py` |

**Exactly what gets recorded, once per turn:** the original message,
the Language Engine's full structured output (detected/reply language,
normalized message, communication style, ambiguity flag+reason, entity
spans), its raw self-reported confidence (explicitly labeled
uncalibrated — see the migration's column comment), which final intent
the turn resolved to, and a `learning_eligible` flag (`False` for
`CREATE_ORDER`/`ORDER_STATUS` — see "what should never be learned
automatically" below).

**What Phase 0 deliberately does NOT do** (do not accidentally start
any of this under a different task without re-reading the full design
first):
- No own Language Brain exists. `brain_used` is always `False`,
  `brain_prediction`/`brain_version` are always `NULL`. There is
  nothing yet to route traffic *to* other than the LLM.
- No confidence calibration. `llm_raw_confidence` is the Language
  Engine's raw, self-reported number — not evidence-based, not
  calibrated against real outcomes.
- No verification step. `verification_result` always stays
  `'unverified'` — nothing yet checks a prediction against a real
  outcome.
- No learning/promotion pipeline. `learning_eligible` is stored but
  **nothing reads it yet** — it's a flag waiting for Phase 3, not an
  active gate on any behavior today.
- No read/query API over `language_experiences` — write-only, exactly
  like `agent_run_traces`. Query it directly with SQL for now.
- Kernel's control flow, prompts, and confidence gate are **unchanged**
  — Phase 0 only exposes data that already existed inside `kernel.py`,
  it does not add a new LLM call or change what the customer sees.

**How to verify Phase 0 is working:**
```
docker compose exec python-api python -m unittest tests.test_language_experience -v
```

## Phase 1 — Passive (Shadow) Brain (done)

Goal: introduce the first version of the own Language Brain, running
**in shadow only** — it must not affect any customer-facing response
yet. All 5 steps complete.

**What was built:**

| Step | Piece | File |
|---|---|---|
| 1 | Embedding provider | reused `app/ai/embedding_service.py` (OpenAI text-embedding-3-small) |
| 2 | `intent_clusters` table (pgvector) | `db/init/011_intent_clusters.sql` |
| 3 | Nearest-example matcher | `app/language/shadow_brain.py` (`ShadowBrain.predict()`) + `shadow_brain_types.py` |
| 4 | Shadow wiring into CoreAgent | `app/agent/core_agent.py` — called after Kernel, logs agreement/disagreement into `language_experiences.brain_prediction` |
| 5 | Exit checkpoint query | see SQL below — requires real traffic |

**Exit checkpoint SQL** (run once real traffic has flowed):
```sql
SELECT final_intent,
       brain_prediction->>'predicted_intent' AS brain_predicted,
       (brain_prediction->>'agreement')::boolean AS agreed,
       COUNT(*) AS n
  FROM language_experiences
 WHERE brain_used = TRUE
GROUP BY 1, 2, 3
ORDER BY 1, n DESC;
```

Tests: `tests/test_shadow_brain.py` (7 tests, all passing).

## Phase 2 — Confidence Calibration + Live Routing (done)

Goal: turn the shadow Brain's raw cosine similarity into an
evidence-based calibrated confidence, then route high-confidence
traffic to the brain (skipping the LLM Intent Engine step).

**What was built:**

| Piece | File | Status |
|---|---|---|
| DB schema (calibration columns + `routing_decisions` + `calibration_runs` tables) | `db/init/012_calibration.sql` | ✅ done |
| Type carriers | `app/language/calibration_types.py` (`IntentCalibrationStats`, `CalibrationResult`, `RoutingDecision`) | ✅ done |
| CalibrationService | `app/language/calibration_service.py` | ✅ done |
| RoutingService | `app/language/routing_service.py` | ✅ done |
| CoreAgent wiring | `app/agent/core_agent.py` — Phase 2 routing decision made before Kernel, `routing_decisions` written after | ✅ done |
| Tests — CalibrationService | `tests/test_calibration_service.py` | ✅ done |
| Tests — RoutingService | `tests/test_routing_service.py` | ✅ done |

**How CalibrationService works:**
1. Reads `language_experiences` rows where `brain_used=TRUE` for the tenant.
2. For each intent, scans similarity thresholds (0.50 → 0.95, step 0.05).
3. Finds the **lowest** threshold where agreement_rate ≥ 90% AND
   sample_count ≥ 30 (more coverage = lower threshold preferred).
4. Writes `calibrated_threshold`, `agreement_rate`, `sample_count`,
   `last_calibrated_at` back to `intent_clusters`.
5. Logs one `calibration_runs` row as an audit trail.

**How RoutingService works** (per turn, in `core_agent.py`):
```
ShadowBrain.predict() → BrainPrediction | None
    ↓
RoutingService.decide()
    if prediction is None                     → 'shadow' (Phase 1 log-only)
    if intent in ROUTING_INELIGIBLE_INTENTS   → 'llm'   (business-risk block)
    if calibrated_threshold is None           → 'llm'   (not yet calibrated)
    if similarity < calibrated_threshold      → 'llm'   (low confidence)
    if similarity >= calibrated_threshold     → 'brain' (high confidence)
    ↓
RoutingDecision written to routing_decisions (before Kernel)
Kernel runs (Phase 3+: skips Intent Engine step when routed_to='brain')
routing_decisions.was_correct updated (after Kernel)
```

**Phase 2 completion note (these were "not yet" at Phase 2 close, all done in Phase 3/4):**
- ✅ **Kernel modified** (Phase 3): `kernel.py` now accepts `predicted_intent_hint`; when
  `routed_to='brain'`, the Intent Engine LLM call is skipped entirely.
- ✅ **Canary traffic split** (Phase 3): `RoutingService._fetch_threshold()` reads
  `canary_splits` and routes `canary_pct`% of brain-eligible traffic to the candidate cluster.
- Calibration schedule was manual — `calibration_scheduler.py` now exists (built 2026-09-28) but **nothing runs it yet**; see "Phase 4 completion" below and `PENDING_WORK.md` C7.

**How to verify Phase 2 is working:**
```
docker compose exec python-api python -m unittest tests.test_calibration_service -v
docker compose exec python-api python -m unittest tests.test_routing_service -v
```

Then, after real traffic has flowed:
```sql
-- How is the routing distributing?
SELECT routed_to, COUNT(*) AS n, AVG(similarity) AS avg_sim
  FROM routing_decisions
 WHERE tenant_id = <your_tenant_id>
GROUP BY routed_to
ORDER BY n DESC;

-- For 'brain' routes: how accurate would they have been?
SELECT predicted_intent, was_correct, COUNT(*) AS n
  FROM routing_decisions
 WHERE tenant_id = <your_tenant_id>
   AND routed_to = 'brain'
GROUP BY predicted_intent, was_correct
ORDER BY predicted_intent, n DESC;
```

**Next concrete step before Kernel modification:**
Accumulate enough `routing_decisions` rows with `routed_to='brain'`
to show `was_correct=TRUE` at a rate ≥ 95% per intent. Once that is
sustained, modify `kernel.py` to accept an optional `predicted_intent`
hint from CoreAgent and skip its own Intent Engine call when the hint
is present.

## Phase 3 — Automated Learning/Promotion Pipeline (done)

Goal: build candidate clusters from real `language_experiences` data
and run a safe shadow-eval → canary → promote/rollback lifecycle so
the Brain continuously improves from real customer traffic.

**What was built:**

| Piece | File | Status |
|---|---|---|
| DB schema (cluster versioning + promotion log + canary_splits) | `db/init/013_cluster_promotion.sql` | ✅ done |
| Type carriers | `app/language/cluster_builder_types.py` | ✅ done |
| ClusterBuilderService | `app/language/cluster_builder_service.py` | ✅ done |
| PromotionService | `app/language/promotion_service.py` | ✅ done |
| RoutingService — canary-aware threshold fetch | `app/language/routing_service.py` | ✅ done |
| Kernel — `predicted_intent_hint` + `intentSource` | `app/kernel/kernel.py`, `app/schemas/kernel.py` | ✅ done |
| Tests — ClusterBuilderService | `tests/test_cluster_builder_service.py` | ✅ done |
| Tests — PromotionService | `tests/test_promotion_service.py` | ✅ done |

**How ClusterBuilderService works:**
1. Reads `language_experiences` rows where `learning_eligible=TRUE`.
2. Groups by `final_intent`. Skips intents with < 50 samples.
3. Embeds all messages for the intent (batch via embedding_service).
4. Computes centroid, selects TOP_K_EXAMPLES (10) nearest-to-centroid.
5. Writes them as `intent_clusters` rows (`source='tenant_learned'`,
   `is_candidate=TRUE`, `is_promoted=FALSE`).
6. Logs a `cluster_promotion_log` row (`event_type='shadow_eval'`).

**How PromotionService works (per intent):**
```
start_canary(tenant_id, intent, candidate_cluster_id, pct=5.0)
    → upserts canary_splits (RoutingService starts routing 5% to candidate)
    → logs 'canary_start'

check_and_promote(tenant_id, intent)
    → reads routing_decisions for canary period (was_correct_rate)
    if sample_count < 20:
        → 'pending' (keep accumulating)
    if was_correct_rate >= 0.95:
        → promote (retire old, is_promoted=TRUE on candidate)
        → clears canary_splits
        → logs 'canary_pass'
    if was_correct_rate < 0.85:
        → rollback (retire candidate, clear canary_splits)
        → logs 'canary_fail'
    else:
        → 'pending' (between thresholds, keep accumulating)
```

**Generalization gate (§5.5, added after Phase 4):** on the promote
branch above, before `_promote()` runs, `_run_generalization_gate()`
scores the candidate against that intent's held-out multilingual cases
(`app/language/generalization_eval.py`, `passes_gate()`):
```
gate pass / intent has no held-out cases (not_applicable)
    → promote (unchanged)
gate fail
    → action 'held_for_generalization': retire candidate, clear
      canary_splits, log 'generalization_fail' (not 'canary_fail')
eval could not run (provider/DB error)
    → action 'pending', retried on the next check_and_promote()
      (never promoted blind, never rolled back on infra failure)
```
Gate thresholds are the uncalibrated defaults of `passes_gate()`;
only 6 intents currently have held-out cases. Manual run:
`python -m app.language.generalization_eval --tenant N [--intent X]`.

**How RoutingService canary routing works:**
When an active `canary_splits` row exists for (tenant, intent), the
threshold fetch uses `random.random() * 100 < canary_pct` to decide
whether this turn uses the candidate's threshold or the promoted
cluster's. This gives the candidate the configured traffic % without
any new infrastructure.

**Next concrete steps (to complete Phase 3):**
1. Build a management API route (or scheduled task) to call
   `ClusterBuilderService.build_for_tenant()` and then
   `PromotionService.start_canary()` per intent.
2. Build a scheduled job to call `PromotionService.check_and_promote()`
   periodically (e.g. daily) and ramp up `canary_pct` when accuracy is stable.
3. Add `CalibrationService` call after cluster promotion so the new
   cluster's threshold is computed immediately.

**Observability queries:**
```sql
-- Promotion history per tenant
SELECT intent, event_type, was_correct_rate, canary_pct, event_at
  FROM cluster_promotion_log
 WHERE tenant_id = <your_tenant_id>
 ORDER BY event_at DESC;

-- Active canaries
SELECT intent, canary_pct, started_at
  FROM canary_splits
 WHERE tenant_id = <your_tenant_id>;

-- Brain vs LLM intent resolution breakdown (Phase 2+3)
SELECT intent_source, COUNT(*) AS n
  FROM agent_run_traces
 WHERE tenant_id = <your_tenant_id>
GROUP BY intent_source;
```

## Phase 4 — Scale/Optimize (in progress)

Goal: make the Brain production-ready across many tenants with
different traffic profiles, risk tolerances, and intent sets.

**What was built:**

| Piece | File | Status |
|---|---|---|
| DB schema (tenant config, novelty_events, conversation_costs) | `db/init/014_phase4_scale_optimize.sql` | ✅ done |
| TenantCalibrationConfig | `app/language/tenant_calibration_config.py` | ✅ done |
| CalibrationService per-tenant support | `app/language/calibration_service.py` | ✅ done |
| PromotionService per-tenant support | `app/language/promotion_service.py` | ✅ done |
| NoveltyDetector | `app/language/novelty_detector.py` | ✅ done |
| ConversationCostTracker | `app/language/conversation_cost_tracker.py` | ✅ done |
| core_agent.py wiring | `app/agent/core_agent.py` | ✅ done |
| Tests — TenantCalibrationConfig | `tests/test_tenant_calibration_config.py` (6 tests) | ✅ done |
| Tests — NoveltyDetector | `tests/test_novelty_detector.py` (7 tests) | ✅ done |
| Tests — ConversationCostTracker | `tests/test_conversation_cost_tracker.py` (7 tests) | ✅ done |

**Feature 1 — Per-tenant calibration config:**
`TenantCalibrationConfig.load(db, tenant_id)` does a PK lookup on
`tenant_calibration_config`. Absent row = all defaults unchanged from
Phase 0-3. CalibrationService and PromotionService both call this at
run start and use the tenant's values instead of module-level constants.
A high-stakes tenant sets `min_agreement_rate=0.95`; a high-volume
low-risk tenant drops to `0.80` for more brain coverage.

**Feature 2 — Novelty detection:**
`NoveltyDetector.check_and_log()` called from `core_agent.py` after
every Kernel run. Uses the `BrainPrediction` already computed by
ShadowBrain (no second embedding call). A message is novel when
`similarity < novelty_threshold` (default 0.50) or no prediction
exists. Novel messages go into `novelty_events` for operator triage:
`triage_result` can be `'new_intent'` (gap in cluster set),
`'existing_intent'` (misrouted), or `'noise'`/`'spam'`.

**Feature 3 — Cost tracking:**
`ConversationCostTracker.record()` upserts `conversation_costs` after
every turn, accumulating `total_input_tokens`, `total_output_tokens`,
and `intent_engine_calls_saved` (incremented when `intentSource='brain'`).
Key observability queries:

```sql
-- Cost trend: brain vs LLM turns per week
SELECT date_trunc('week', last_turn_at) AS week,
       SUM(intent_engine_calls_saved)   AS brain_turns,
       SUM(turn_count)                  AS total_turns,
       ROUND(100.0 * SUM(intent_engine_calls_saved)
             / NULLIF(SUM(turn_count), 0), 1) AS brain_pct
  FROM conversation_costs
 WHERE tenant_id = <your_tenant_id>
GROUP BY 1 ORDER BY 1 DESC;

-- Cost per resolved conversation
SELECT date_trunc('week', resolved_at)            AS week,
       COUNT(*)                                    AS resolved_convos,
       AVG(total_input_tokens + total_output_tokens) AS avg_tokens
  FROM conversation_costs
 WHERE tenant_id = <your_tenant_id>
   AND resolved = TRUE
GROUP BY 1 ORDER BY 1 DESC;

-- Untriaged novelty events
SELECT best_intent, COUNT(*) AS n, AVG(best_similarity) AS avg_sim
  FROM novelty_events
 WHERE tenant_id = <your_tenant_id>
   AND triaged   = FALSE
GROUP BY best_intent
ORDER BY n DESC;
```

**What Phase 4 deliberately does NOT do yet:**
- ✅ *(built 2026-09-28, see "Phase 4 completion")* **Admin API route** for `tenant_calibration_config` (was: currently
  requires manual SQL `INSERT/UPDATE`). A management route at
  `PUT /admin/tenants/{id}/calibration-config` is the next step.
- ✅ *(API built 2026-09-28; no UI)* **Novelty triage UI** — `novelty_events` has all the triage columns
  (`triaged`, `triage_result`, `triage_intent`, `triaged_by`) but no
  UI or API to set them. A minimal triage endpoint is next.
- ✅ *(job built 2026-09-28, not scheduled)* **Automated canary ramp** — `canary_pct` was set once by
  `PromotionService.start_canary()` and never auto-incremented.
  A scheduler that bumps `canary_pct` (5→20→50→100%) as accuracy
  stays above threshold is a natural follow-on.
- **cost_per_token pricing** — `conversation_costs` tracks raw tokens.
  Multiplying by model pricing to get USD amounts is a follow-on.

**How to run all Phase 4 tests:**
```bash
docker compose exec python-api python -m unittest \
    tests.test_tenant_calibration_config \
    tests.test_novelty_detector \
    tests.test_conversation_cost_tracker -v
```

## ─── HANDOFF DOCUMENT ─────────────────────────────────────────────

**For the next session starting Phase 4 completion or any new work:**

### Repository state at handoff

| Phase | Status |
|---|---|
| Phase 0 — Foundation (language_experiences logging) | ✅ done |
| Phase 1 — Shadow Brain (nearest-example matcher) | ✅ done |
| Phase 2 — Calibration + Live Routing (kernel modified) | ✅ done |
| Phase 3 — Cluster building + canary promotion pipeline | ✅ done |
| Phase 4 — Per-tenant config + novelty + cost tracking | ✅ core done, API/UI next |

### All language/ files and their roles

| File | Role |
|---|---|
| `shadow_brain.py` | Phase 1: nearest-example matcher (pgvector cosine) |
| `shadow_brain_types.py` | BrainPrediction dataclass |
| `experience_service.py` | Phase 0: write language_experiences rows |
| `experience_types.py` | LanguageExperience dataclass |
| `calibration_service.py` | Phase 2: compute per-intent similarity thresholds |
| `calibration_types.py` | IntentCalibrationStats, CalibrationResult, RoutingDecision |
| `routing_service.py` | Phase 2+3: brain/llm/shadow routing decision per turn |
| `cluster_builder_service.py` | Phase 3: build tenant_learned candidate clusters |
| `cluster_builder_types.py` | ExperienceRow, BuiltCluster, ClusterBuildResult, PromotionCheckResult |
| `promotion_service.py` | Phase 3: canary → promote/rollback lifecycle |
| `tenant_calibration_config.py` | Phase 4: per-tenant threshold overrides |
| `novelty_detector.py` | Phase 4: log messages below novelty threshold |
| `conversation_cost_tracker.py` | Phase 4: upsert conversation token spend |

### DB migrations in order

`001` core → `010` language_experiences → `011` intent_clusters (pgvector)
→ `012` calibration (routing_decisions, calibration_runs)
→ `013` cluster_promotion (versioning, canary_splits)
→ `014` phase4 (tenant_calibration_config, novelty_events, conversation_costs)

### What the next session should build first

1. `app/api/routes/admin_calibration.py`
   `PUT /admin/tenants/{id}/calibration-config` — update
   `tenant_calibration_config` row via `TenantCalibrationConfig.save()`.
   `GET /admin/tenants/{id}/calibration-config` — read current config.

2. `app/api/routes/admin_novelty.py`
   `GET /admin/tenants/{id}/novelty-events` — list untriaged events
   with pagination.
   `PATCH /admin/novelty-events/{id}` — set triage_result, triage_intent.

3. `app/language/canary_ramp_service.py`
   Out-of-band scheduler: reads `canary_splits`, checks
   `routing_decisions.was_correct_rate`, auto-increments `canary_pct`
   (5→20→50→100%) when accuracy stays above `min_promote_accuracy`,
   then calls `promotion_service.check_and_promote()`.

4. `app/language/calibration_scheduler.py`
   Thin wrapper that calls `calibration_service.run_for_tenant()` for
   all active tenants (query: `SELECT id FROM tenants WHERE is_active=TRUE`).
   Meant to run daily via APScheduler or a cron job in the container.

### Key constants to know (all in their respective service files)

| Constant | Default | Where |
|---|---|---|
| MIN_AGREEMENT_RATE | 0.90 | calibration_service.py |
| MIN_SAMPLE_COUNT | 30 | calibration_service.py |
| THRESHOLD_SCAN_MIN/MAX/STEP | 0.50/0.95/0.05 | calibration_service.py |
| MIN_PROMOTE_ACCURACY | 0.95 | promotion_service.py |
| MIN_ROLLBACK_ACCURACY | 0.85 | promotion_service.py |
| MIN_CANARY_SAMPLES | 20 | promotion_service.py |
| TOP_K_EXAMPLES | 10 | cluster_builder_service.py |
| MIN_SAMPLES_TO_BUILD | 50 | cluster_builder_service.py |
| NOVELTY_THRESHOLD default | 0.50 | tenant_calibration_config.py |
| BRAIN_INJECTED_CONFIDENCE | 0.95 | kernel/kernel.py |

All of the above are overridable per-tenant via `tenant_calibration_config`
(Phase 4). Module-level constants are the fallback when no row exists.

## Business-risk exclusion — CORRECTED scope (see GENERAL_LANGUAGE_BRAIN.md §3)

**This section describes a conflation that the corrected architecture
fixes — read `docs/GENERAL_LANGUAGE_BRAIN.md` §3 and §3.1 before
changing this code.** As currently implemented, `CREATE_ORDER` and
`ORDER_STATUS` are marked `learning_eligible=False` at write time
(`core_agent.py`'s `_LEARNING_INELIGIBLE_INTENTS`) and excluded from
routing to the brain at decision time (`routing_service.py`'s
`ROUTING_INELIGIBLE_INTENTS`) — the same flag/set doing both jobs.

**That single flag is wrong for language learning.** A message like
*"amar parcel ta koi?"* is excellent material for learning the
Banglish ORDER_STATUS *language pattern*, even though the system must
never autonomously *act* on an order merely because the language is
understood. The corrected model (`GENERAL_LANGUAGE_BRAIN.md` §3) splits
this into four independent flags — `language_learning_eligible`,
`automation_eligible`, `promotion_eligible`, `training_eligible` — and
only `automation_eligible` should stay `FALSE` for these two intents
permanently. `language_learning_eligible` should be `TRUE` for them
like any other intent.

**Not changed in this task** (documentation-only scope) — this is
`GENERAL_LANGUAGE_BRAIN.md` §9's item 1, the top-priority next
implementation step: split `_LEARNING_INELIGIBLE_INTENTS` out of
`core_agent.py` entirely (stop excluding these intents from
`language_experiences.learning_eligible`), rename/repurpose
`ROUTING_INELIGIBLE_INTENTS` to mean automation-authorization only (not
understanding-routing), and keep a permanent
`AUTOMATION_INELIGIBLE_INTENTS` block that only the future Tool Engine
checks before executing an action. A tenant that later wants to opt
into automation for these must still make a deliberate, tenant-level
configuration change — not a side effect of the Brain improving —
that part of the original guidance is unchanged.

## Phase 4 extended scope — implementation status

| Item | §9 ref | Status | Files changed |
|---|---|---|---|
| Split control planes (`language_learning_eligible` vs `automation_eligible`) | §9 item 1 | ✅ done | `app/language/control_plane.py` (new), `core_agent.py`, `routing_service.py`, `kernel.py` |
| Add `verification_level` to `language_experiences`, gate `ClusterBuilderService` | §9 item 2 | ✅ done | `015_language_verification.sql`, `experience_types.py`, `experience_service.py`, `cluster_builder_service.py` |
| Extend Language Engine JSON schema — `script`/`is_transliterated`/`transliterated_from`/`code_mixing` | §9 item 3 | ✅ done | `016_language_schema_v2.sql`, `language_types.py`, `language_engine.py`, `experience_types.py`, `experience_service.py`, `schemas/kernel.py`, `kernel/kernel.py`, `core_agent.py`, 11 new tests in `test_language_engine.py` |
| Phase 5 — General Language Brain umbrella (capability A formalized) | §7.1 Phase 5 | 🟡 in progress | see Phase 5 section below |

## Phase 5 — General Language Brain Umbrella

### Objective

Formalize capability A (§1.1) as an orchestration layer over B–M, not
just B. The immediate Phase 5 work has two parallel tracks:

**Track 1 — `detectedLanguage` closed enum → open language tag
migration (§5.1–5.2):**

`detectedLanguage: "bn" | "en" | "mixed" | "other"` is the last
remaining closed-enum ceiling in the Language Engine. The `language`
open-tag field (added additively in Phase 4 extended scope §9 item 3)
is the replacement. Migration steps:

| Step | Status | Notes |
|---|---|---|
| Audit: find all code branching on `detectedLanguage` values | ✅ done | No `if detected_language ==` branches exist anywhere. The field is only passed through (kernel.py → KernelRunResponse → core_agent.py → LanguageExperience). No callers make routing decisions on its value. Safe to migrate. |
| Prompt: demote `detectedLanguage` to legacy-only annotation | ✅ done (Phase 4 §9 item 3 second pass) | Prompt already says "LEGACY field kept for backward compatibility only" |
| DB: add `language` column to `language_experiences` | ✅ done | `db/init/017_language_tag.sql` |
| API: expose `language` in `KernelRunResponse` | ✅ done | `schemas/kernel.py` |
| Downstream: confirm no consumer switches on `detectedLanguage` values | ✅ confirmed this session | `routing_service.py`, `context_engine.py`, `shadow_brain.py`, `promotion_service.py` — none branch on the value |
| Phase 5 formal deprecation notice | ✅ done | Deprecation comments already present on `KernelRunResponse.detectedLanguage` (`schemas/kernel.py`) and `LanguageResult.detected_language` (`language_types.py`), pointing to `.language`. (This row previously said ⬜ next — doc was stale.) |
| Understanding report (new) | ✅ built, ⬜ untested against real DB | `understanding_report.py` + `GET /language/understanding/report`; see `PENDING_WORK.md` C1. |
| Calibration / review / removal tooling (new) | ✅ tooling, ⬜ jobs | `calibration_apply.py`, `eval_set_review.py`, `detected_language_audit.py` — see `docs/PENDING_WORK.md` A1–A3. Nothing calibrated, reviewed or removed yet. |
| Turn understanding persistence (new) | ✅ built, ⬜ not applied/scheduled | `db/init/018_turn_understandings.sql`, 90-day retention; see `PROJECT_STATUS.md` and `PENDING_WORK.md` C1/C1a. |
| Capability A umbrella (new) | ✅ partial (now includes the `novel` phenomenon via `NoveltyOutcome`) | `app/language/glb.py` + `tests/test_glb.py`; see `PROJECT_STATUS.md`. Orchestration not built — `docs/PENDING_WORK.md` C1. |
| Removal readiness check (new) | ✅ built, ⬜ not yet run on real traffic | `app/language/language_tag_readiness.py` — `python -m app.language.language_tag_readiness --since <date> [--tenant N]` prints READY/NOT READY (exit 0/1). Rule: enough real rows in the window (placeholder `MIN_ROWS_FOR_DECISION=500`) AND zero `language='und'`. Rows before migration 017 are `'und'` forever, hence the `--since` window. Tests: `tests/test_language_tag_readiness.py` (7). |
| Formal removal of `detectedLanguage` | ⬜ Phase 6 — BLOCKED on real traffic | Do only after the readiness check reports READY. Not done, deliberately. Scope when unblocked: drop the field from the Language Engine prompt/parser, `LanguageResult`, `KernelRunResponse`, `LanguageExperience`, `core_agent.py`, and a new migration for the `language_experiences.detected_language` column; update `docs/LANGUAGE.md`, `KERNEL.md`, `AGENT.md`, and the tests that pass it. |

**Track 2 — Eval set full coverage (§5.5 generalization gate):**

The `multilingual_intents_v1.json` held-out eval set must cover all
10 `IntentType` values so `PromotionService`'s generalization gate is
not silently skipped for half the intent space.

| Item | Status | Files |
|---|---|---|
| Eval cases for COMPLAINT, DELIVERY_INFO, ORDER_STATUS, PRICE_INQUIRY, PRODUCT_AVAILABILITY, RETURN_REQUEST (6 intents × 13 languages) | ✅ done (prior session) | `eval_sets/multilingual_intents_v1.json` |
| Eval cases for PRODUCT_INFO, NEGOTIATION, CREATE_ORDER, GENERAL_QUESTION (4 intents × 13 languages = 52 new cases) | ✅ done this session | `eval_sets/multilingual_intents_v1.json` (78 → 130 cases; 10 intents now all covered) |
| `test_generalization_eval.py` — concept_consistency fraction updated (5/6 → 9/10) | ✅ done this session | `tests/test_generalization_eval.py` |
| Threshold calibration on real tenant data | 🟡 tooling done, ⬜ values NOT calibrated | The thresholds are now named module constants (`MIN_OVERALL_ACCURACY`, `MIN_LANG_ACCURACY`, `MIN_COVERAGE`, `MIN_CONCEPT_CONSISTENCY`) plus a `THRESHOLDS_CALIBRATED = False` flag; `passes_gate()` defaults read them. The doc's old names `MIN_LANG_ACCURACY`/`MIN_COVERAGE` did not exist in code (it had `min_per_language`/`min_cases_per_language`) — now they do. New CLI flags: `--json` (save a run as evidence) and `--suggest` (report + candidate thresholds from `suggest_thresholds()`, never edits files). Tests: `test_generalization_eval.py` (20 → 30). |

### Calibration runbook (do this once a tenant has real clusters)

1. `python -m app.language.generalization_eval --tenant N --json > calib_YYYYMMDD.json` (repeat for 2–3 tenants/dates; keep the files).
2. `python -m app.language.generalization_eval --tenant N --suggest` — refuses (`ok: false`) below 100 scored cases rather than inventing numbers.
3. Read the misses first. A low language score usually means a bad cluster or a bad eval case (the set is AI-drafted, not native-speaker reviewed) — fix that, don't lower the bar.
4. Copy the suggested values into the constants, set `THRESHOLDS_CALIBRATED = True`, and add a row to the log below **in the same change**.

| Date | Tenant(s) | Scored cases | Observed overall / weakest lang | Constants set | Reviewer |
|---|---|---|---|---|---|
| — | — | — | — | *(no real run yet — placeholders in force)* | — |

### Eval set — per-intent language matrix (v1, 130 cases)

All 10 intents × 13 language/script variants each:
`en` · `bn` (Bengali script) · `bn-latn` (Banglish — transliterated) ·
`hi` (Devanagari) · `hi-latn` (Hinglish — transliterated) · `ar` (Arabic script) ·
`ar-latn` (transliterated) · `es` · `zh` (Han) · `ko` (Hangul) · `th` (Thai) ·
`ru` (Cyrillic) · `tr`

Each concept_id appears with identical `(language, script, transliterated)` variants,
so the leakage guard and the transliterated slice both work correctly across all intents.

## Phase 4 completion — built 2026-09-28 (the four "build first" items)

Tested with 509 unit tests against throwaway stubs (no Postgres, no real
FastAPI/pydantic/SQLAlchemy — same caveat as `PENDING_WORK.md` B1). **None
of it has run against a real database or been hit over HTTP yet.**

| Piece | File | Tests |
|---|---|---|
| Calibration-config admin routes | `app/api/routes/admin_calibration.py` | `test_admin_calibration.py` (36) |
| Novelty triage admin routes | `app/api/routes/admin_novelty.py` | `test_admin_novelty.py` (33) |
| Auto canary ramp job | `app/language/canary_ramp_service.py` | `test_canary_ramp_service.py` (57) |
| Daily calibration job | `app/language/calibration_scheduler.py` | `test_calibration_scheduler.py` (17) |
| Single-run guard (advisory lock) | `app/language/scheduler_lock.py` | in `test_canary_ramp_service.py` |
| `PromotionService.abort_canary()` (new, additive) | `app/language/promotion_service.py` | in `test_canary_ramp_service.py` |

All routes are registered in `app/main.py` and gated by the internal
secret (no dashboard role layer yet — `ROADMAP.md` §1).

**Routes**
- `GET /admin/tenants/{id}/calibration-config` — effective config, `isDefault` when no row is stored, plus the allowed bounds.
- `PUT /admin/tenants/{id}/calibration-config` — **partial merge** (only the fields sent change; `null` = not sent; unknown fields rejected). Bounds are policy choices in `admin_calibration.BOUNDS` (only the 0.70–0.99 agreement range comes from migration 014); `minRollbackAccuracy < minPromoteAccuracy` is enforced. Reads strictly (a DB error is a 500, never "defaults"), because a merge on top of silently-defaulted values would overwrite real tuning. Changes are logged (old → new); there is no audit table.
- `GET /admin/tenants/{id}/novelty-events?status=untriaged|triaged|all&limit=&offset=` — newest first, with `total`.
- `PATCH /admin/novelty-events/{eventId}?tenantId=` — sets the triage columns (`triageResult`: new_intent | existing_intent | noise | spam; `existing_intent` needs a known `triageIntent`; `triagedBy` is a free-text label, not authenticated identity). **Triage only records the verdict — nothing reads it yet**; `ClusterBuilderService` still learns only from `language_experiences`.

**Jobs (run by cron/compose; the app does not start them)**
```
python -m app.language.calibration_scheduler [--tenant N] [--dry-run] [--loop --interval-hours 24]
python -m app.language.canary_ramp_service   [--tenant N] [--dry-run] [--loop --interval-minutes 60] [--min-dwell-hours 24]
```
- *Calibration:* calls `CalibrationService.run_for_tenant()` per active tenant, fresh session each. It changes live routing (thresholds can be NULLed → intent goes back to the LLM). It cannot distinguish "no shadow data" from "service swallowed an error" (both empty). `--dry-run` only lists tenants with `brain_used` rows.
- *Canary ramp:* ladder 5 → 20 → 50 → 100. Per stage it needs ≥ 20 samples **since the stage started**, ≥ 24 h dwell, and accuracy ≥ the tenant's `min_promote_accuracy` to advance; accuracy < `min_rollback_accuracy` (with ≥ 20 samples) aborts immediately via `PromotionService.abort_canary()` (logged `canary_fail`). Only at 100% does it call `check_and_promote()` (so the §5.5 generalization gate still decides promotion). Reaching 100% restarts the evidence window (`canary_splits.started_at = NOW()`). Steps are logged to `cluster_promotion_log` with the new `event_type='canary_ramp'`. Do not call `check_and_promote()` by hand on a ramping canary — it measures the whole window and can promote at 5%.
- Both use a Postgres advisory lock (`scheduler_lock.py`) so overlapping runs skip.

**Former limitation, FIXED 2026-09-28 (`PENDING_WORK.md` C8):** `routing_decisions`
now records the serving cluster/branch (`019_routing_served_branch.sql`) and
the ramp counts only candidate-served turns, so every stage measures the
candidate itself. Pre-019 rows are NULL and never counted.
