# docs/PENDING_WORK.md

Single place for work that is **known, specified, and not done** —
each item says why it is blocked, how to unblock it, and how to know it
is finished. Nothing here is "in progress" silently. When an item is
done: tick it, add the evidence (date, command output summary,
commit), and update `PROJECT_STATUS.md` in the same change.

Last updated: 2026-09-29 (session 6).

> **Governing task spec + required audit for Language Intelligence data
> work: `docs/TRAINING_GRADE_DATA_TASK.md` + `docs/TRAINING_GRADE_DATA_AUDIT.md`
> (Current State Matrix / Minimal Change Plan / Gap Report, kept current
> every session — read it first).**

> **Deduplication + retention design for `language_experiences`
> (no retention today, no dedup today): `docs/EXPERIENCE_DEDUPLICATION_AND_RETENTION.md`
> (tasks P6D-1..P6D-8). Design decided, not yet built.**

> **Phases 5–8 master plan (IDs P5-*…P8-*, order of work, done-criteria):
> `docs/PHASE_5_8_PLAN.md`. What real traffic must produce and which
> checks to run when: `docs/REAL_TRAFFIC_DATA_COLLECTION.md`.**

## A. Blocked on REAL DATA (cannot be done in a build/CI environment)

### A1. Calibrate generalization-gate thresholds
- **Status:** ⬜ NOT done and cannot be done without real data. Placeholders
  in force; `THRESHOLDS_CALIBRATED = False` in
  `python-api/app/language/generalization_eval.py`.
- **Blocked on:** a tenant with real `intent_clusters` + embedding
  provider key + LLM key. **Also weakened by A3** (unreviewed eval set).
- **Tooling ready (2026-09-28):** `app/language/calibration_apply.py`
  turns saved `--suggest` outputs into the constants + the log row,
  mechanically. It refuses: <2 real runs, any run with `ok:false`
  (<100 scored cases), runs whose thresholds disagree by >0.15, and an
  unreviewed eval set unless `--accept-unreviewed-set` is passed (the
  log row then says so). Dry run by default; `--write --reviewer NAME`
  applies. Tests: `test_calibration_apply.py` (14, fabricated data).
- **Steps:**
  1. `python -m app.language.generalization_eval --tenant N --suggest > runN.json` for ≥2 tenants/dates.
  2. Read the misses in each file first (bad cluster vs bad case).
  3. `python -m app.language.calibration_apply run1.json run2.json` (dry run), then add `--write --reviewer "Name"`.
  4. Commit the changed `generalization_eval.py` + the log row together.
- **Done when:** the calibration log has a real row and
  `THRESHOLDS_CALIBRATED = True`.

### A2. Remove legacy `detectedLanguage` (Phase 6)
- **Status:** ⬜ NOT done, deliberately: the documented gate is "real
  traffic has `language != 'und'`", and no such traffic exists to check.
  Removing it now would break compatibility for no benefit.
- **Blocked on:** real post-migration-017 traffic.
- **Unblock check:** `python -m app.language.language_tag_readiness --since <date after 017 went live> [--tenant N]` must print READY (exit 0).
- **Scope inventory (generated, 2026-09-28):**
  `python -m app.language.detected_language_audit`. It found **more than
  this doc's first version listed** — the trace pipeline and the
  verification service also carry the field. Code that must change:
  - `python-api/app/language/language_engine.py` (prompt schema + parser; note the `or "other"` default)
  - `python-api/app/language/language_types.py` (`LanguageResult.detected_language`, a *required* `str`)
  - `python-api/app/kernel/kernel.py` (log line + 2 `KernelRunResponse(...)` builds)
  - `python-api/app/schemas/kernel.py` (`KernelRunResponse.detectedLanguage`)
  - `python-api/app/agent/core_agent.py` (trace write, the experience write, `metadata.language.detected`, and the gate below)
  - `python-api/app/trace/trace_types.py`, `python-api/app/trace/trace_service.py` (`agent_run_traces.detected_language`)
  - `python-api/app/language/experience_types.py`, `experience_service.py`, `verification_service.py` (`language_experiences.detected_language`)
  - `db/init/005_agent_foundation.sql` and `db/init/010_language_experience.sql` → one NEW migration dropping both columns (never edit old migrations)
  - tests: `test_language_engine.py` (34 refs), `test_language_experience.py`, `test_verification_service.py`, `test_core_agent_understanding.py`
  - docs: README, `LANGUAGE.md`, `KERNEL.md`, `AGENT.md` and status docs
  - node-api: **0 readers found** (re-run the audit before removal).
- **TRAP — do not do a naive delete.** `core_agent.py` decides whether to
  write a `language_experiences` row with
  `if kernel_result.detectedLanguage is not None`. Switching that gate
  to `language` changes which turns are recorded: a turn where the
  engine returned the legacy field but no open tag (`language == 'und'`)
  would silently stop being recorded. Decide the gate's new meaning
  explicitly (e.g. "language engine ran successfully") and add a test
  before touching it. Also: `language_experiences` rows written before
  017 are `'und'` forever, so the drop migration must not assume
  backfill.
- **Done when:** the audit shows no code references, and the Docker
  suite passes.

### A3. Native-speaker review of the eval set
- **Status:** ⬜ NOT done and cannot be done by the assistant — it needs
  human native speakers (bn, hi, ar, es, zh, ko, th, ru, tr, plus the
  romanized bn/hi/ar forms). `reviewed_by_native_speakers` stays `false`.
- **Tooling ready (2026-09-28):** `app/language/eval_set_review.py`
  - `lint` — automated structural checks (script vs declared script,
    transliteration flags, duplicates, missing concept variants). Ran on
    the real set: **clean**. This is *not* a language review.
  - `export --out review_sheets/` — one CSV per language+script (13),
    each row with the English reference text.
  - `apply sheet.csv …` — validates (reviewer name required, verdict
    `ok|fix|reject`, `fix` needs corrected text), applies fixes, records
    reviewer + date per case in
    `eval_sets/multilingual_intents_v1.review.json`, and flips the
    top-level flag to `true` **only** when every case is ok/fix with a
    named reviewer.
  - `status` — per-variant progress.
  Tests: `test_eval_set_review.py` (21, synthetic data).
- **Done when:** all 130 cases have a named native reviewer and the flag
  is `true` (set by the tool, never by hand).

### A4. Agreement-rate measurement against real traffic (Phase 1 exit checkpoint)
- **Status:** ⬜ never done (`PROJECT_STATUS.md`, Shadow Brain row).
- **Blocked on:** real `language_experiences` with `brain_used = TRUE`.

## B. Blocked on ENVIRONMENT (needs Docker / Postgres / network)

### B1. Full Python suite has only run against throwaway stubs
On 2026-09-28 all 21 test files passed, but with hand-written stand-ins
for `sqlalchemy`, `pydantic`, `pydantic_settings`, `openai` and
`anthropic` (the build sandbox has no network/pip) — NOT the real
libraries. Stubs can hide real-library behavior (pydantic validation,
SQLAlchemy `text()` binding). Before trusting this phase, run in Docker:
`docker compose exec python-api python -m unittest discover -s tests -t .`
and record the result here. Still never run at all: `node-api` tests.

### B2. Live end-to-end test (Postgres + Redis + Meta) — see `ROADMAP.md` §6.

## C. Designed, not built (code work — no data needed)

### C1. Capability A orchestration — persistence ✅ done, orchestration ⬜
**Done 2026-09-28 (retention decided: 90 days):** `TurnUnderstanding` is
now persisted per turn to `turn_understandings`
(`db/init/018_turn_understandings.sql`, `app/language/understanding_store.py`,
wired in `core_agent.py`, isolated and never raises, rolls back on
failure). Metadata only — no message text, no person-inference columns
(tests pin both). `expires_at` is set at insert; a test fails if the
SQL's `INTERVAL '90 days'` and `TURN_UNDERSTANDING_RETENTION_DAYS`
ever disagree. Tests: `test_understanding_store.py` (17),
`test_core_agent_understanding.py` (8).
**First reader built:** `app/language/understanding_report.py` +
`GET /language/understanding/report?tenantId=&days=` (internal-secret
gated, tenant-scoped, aggregated in SQL, ≤90 days) and
`python -m app.language.understanding_report --tenant N --days 30`:
language/script mix, code-mixed / transliterated / ambiguous / novel /
low-confidence shares, brain-vs-LLM intent attribution, non-automatable
share, and factual warnings (small sample, unresolved language). It
judges nothing — thresholds need real data. Tests:
`test_understanding_report.py` (14). The **route itself is only
structurally tested** (FastAPI is not installed in the build sandbox) —
hit it once in Docker and record that here.
**Orchestration — first step DONE 2026-09-29:** `app/language/glb_orchestrator.py`
`plan_turn()` consumes `TurnUnderstanding` and returns an **advisory**
`TurnPlan` (teacher required?, human review?, clarify?, triage?, record
as learning material?), exposed as `metadata.plan`. It is recorded, never
enacted (`enacted` is always False, pinned by test); nothing in the
Kernel/Routing reads it. **Still to do:** compare plans with what actually
happened on real traffic, then decide (Phase 7) whether any rule may act.
Tests: `test_glb_orchestrator.py` (16). Spec: `GENERAL_LANGUAGE_BRAIN.md` §1.1, §7.1.

### C1a. Schedule the 90-day purge (`scripts/migrate.sh` + `deploy/crontab.example` added 2026-09-29)  ⬜ NOT scheduled — table grows until done
The purge exists (`python -m app.language.understanding_retention`,
`--dry-run` first) but **nothing runs it**; the app has no scheduler.
Add a daily job (host cron / pg_cron / orchestrator), then record here
which one and when. Also apply migration 018 to any existing database
(`db/init` only auto-runs on a fresh volume).
**Ready-to-paste (host cron, daily 03:15, from the compose directory):**
`15 3 * * * cd /path/to/project && docker compose exec -T python-api python -m app.language.understanding_retention >> /var/log/understanding_retention.log 2>&1`
(pg_cron cannot run the Python CLI; if you prefer it, schedule the
equivalent `DELETE FROM turn_understandings WHERE expires_at < CURRENT_TIMESTAMP`
in batches — the CLI batches for you.) Run with `--dry-run` once first.
**Done when:** a scheduled job exists and one run's output is logged here.

### C1d. ✅ DONE 2026-09-29 — persist the advisory plan (P5-4a)
`db/init/026_turn_plan.sql` adds nullable `plan_understanding_source`,
`plan_automation`, `plan_clarify`, `plan_triage`, `plan_record_as_learning`,
`plan_reasons` to `turn_understandings` (same row, same 90-day purge).
`record_turn_understanding(..., plan=plan)` writes them; on failure it
retries ONCE without the plan so deploying code before the migration
cannot drop the understanding row. Tests: `test_turn_plan_persistence.py` (15).
**Still to do (needs Docker):** `scripts/migrate.sh 026`; confirm a real
turn writes non-NULL `plan_*` values. Rows before 026 are NULL forever.
P5-4 (compare plan vs outcome) stays blocked on real traffic.

### C1b. Decide whether the retention should be per-tenant
90 days is global. A tenant-specific value would go in
`tenant_calibration_config` and `expires_at` (already per-row, so no
schema change to the new table). Not needed until a tenant asks.

### C1c. Use the report to steer the blocked work (once real traffic exists)
Run the report per tenant, then: prioritise native review (A3) and eval
cases (C3) for the language/script variants with the most traffic;
cross-check `language_unresolved` against `language_tag_readiness` (A2);
look at `novel` and `code_mixed` shares before building any mechanism
for them (§5.4). Nothing to build — a decision procedure.

### C2. ✅ DONE 2026-09-28 — `is_novel` into the understanding record
`NoveltyDetector.check()` now returns `NoveltyOutcome(is_novel, logged)`
(`is_novel=None` = could not be determined); `check_and_log()` kept as a
wrapper. `core_agent.py` passes `is_novel` into `glb`. Also fixes a
blind spot: a novel message from a tenant with novelty logging disabled
used to be indistinguishable from "not novel". Tests:
`test_novelty_detector.py` (12), `test_core_agent_understanding.py` (6).
Remaining sliver: `is_novel` is only computed inside the
language-gated block; a fallback turn is always `False` (= unknown).

### C3. Eval cases beyond intent (§5.4 phenomena)
Only intent generalization is measured. No cases for entities,
corrections, ellipsis, emoji, typos, dialect.

### C4. Global-vs-tenant learning layer (§4) — Phase 7. No global table exists.

### C5b. Teacher→student training plan: `docs/OWN_LANGUAGE_MODEL_TRAINING_PLAN.md` (P6T-*). **Decision P6T-D1 (does the LLM provider's terms allow training an own model on its outputs?) must be settled before any real distillation run (A5).**

**P6R-1 knowledge ingestion DONE 2026-09-29** (`rag/ingestion.py`, `/knowledge/documents`, migration `025`, 29 tests). **Still open:** apply 025 (`scripts/migrate.sh 025`), hit the three routes once in Docker with a real `OPENAI_API_KEY` and record it here, upload one real tenant document and confirm `/rag/search` returns it; PDF/DOCX parsing; re-embedding job when `EMBEDDING_MODEL` changes; a dashboard upload UI.

### C5a. ⚠ Own REPLY generation was never planned — now Phase 6B (`PHASE_5_8_PLAN.md` P6R-*). Decision P6R-D1 (may reply text be stored for learning) blocks the capture work; knowledge ingestion (P6R-1) does not.

### C5. Phase 6 own-model distillation, Phase 7 adaptive routing,
Phase 8 pricing table/drift detection — `GENERAL_LANGUAGE_BRAIN.md` §7.1.

### C6. Older items still open (unchanged, see `ROADMAP.md`)
Knowledge ingestion pipeline (RAG empty) · Tool/Decision Engine ·
role-based permissions · Facebook Messenger · plaintext
`channel_credentials.access_token` · payment provider · frontend ·
`agent_run_traces` read API · cost tracking.

### C7. Schedule the new jobs  ⬜ NOT scheduled (built 2026-09-28)
`app/language/calibration_scheduler.py` and `app/language/canary_ramp_service.py`
exist and are unit-tested, but nothing runs them. Run each with `--dry-run`
once in Docker first (also the first real check of the SQL and of the
advisory lock: run the same job twice at once — the second must report
`skipped`). Ready-to-paste host cron (from the compose directory):
`45 3 * * * cd /path/to/project && docker compose exec -T python-api python -m app.language.calibration_scheduler >> /var/log/calibration_scheduler.log 2>&1`
`5 * * * * cd /path/to/project && docker compose exec -T python-api python -m app.language.canary_ramp_service >> /var/log/canary_ramp.log 2>&1`
Also hit the four admin routes once (`/docs`) and record it here — real
FastAPI/pydantic request parsing (`extra="forbid"`) has not been exercised.
**Done when:** both jobs are scheduled and one logged run of each is recorded here.

### C8. ✅ DONE 2026-09-28 — record which branch served a turn on `routing_decisions`
`db/init/019_routing_served_branch.sql` adds `served_cluster_id` and
`served_branch` (`candidate` | `promoted` | `other` | NULL).
`RoutingService._fetch_serving()` (new; `_fetch_threshold` is now a thin
2-tuple wrapper) reports which cluster's threshold decided the turn;
`core_agent._record_routing_decision` writes it; the ramp's `_STAGE_STATS`
now counts only `served_branch='candidate'` rows of *this* canary's
candidate cluster. Tests: `test_routing_served_branch.py` (20, incl. a
check that the migration and code agree).
**Consequences:** rows from before 019 are NULL forever and are never
counted, so an in-flight canary restarts its stage evidence from zero after
deploy; low stages need proportionally longer to reach 20 samples.
`PromotionService.check_and_promote()` still measures the whole window
unfiltered — fine at 100% (the only stage where the ramp calls it), still
wrong if a human calls it mid-ramp (already warned against).
**Still to do (needs Docker):** apply 019 to any existing database
(`db/init` only auto-runs on a fresh volume) and run a canary once.

### C10. Phase 6 — own model learning (started 2026-09-29)
Built (all pure/faked, **not run on real data, not wired into serving**):
`intent_model.py` (softmax regression on embeddings + temperature
calibration, JSON artifact), `distillation_dataset.py` (leak-free,
tenant-isolated, deterministic split from *verified* experiences),
`model_eval.py` (accuracy/F1/ECE/per-language/coverage vs a
nearest-centroid baseline; `THRESHOLDS_CALIBRATED = False`),
`model_registry.py` + migration `020_language_models.sql` (lifecycle
trained→shadow→canary→active→retired; one active per tenant),
`train_intent_model.py` (job; refuses with too little data).
Tests: `test_intent_model` (13), `test_distillation_dataset` (14),
`test_model_eval` (16), `test_model_registry` (16), `test_train_intent_model` (9).
**Shadow runner DONE 2026-09-29** (`model_shadow.py`, migration 021, flag `MODEL_SHADOW_ENABLED` default OFF, `test_model_shadow.py`). **Still to build (Phase 6 remainder):** model *canary* + rollback (mirror `canary_ramp_service`), a serving
hook behind `routing_service` — all only after A5 below shows the model
beats the baseline on real data. Also a stronger model class if softmax
regression proves insufficient (§6 defers the technology choice).

**Serving hook DONE 2026-09-29 (P6-4)** — `model_serving.py`, migration `024`, flags `MODEL_SERVING_ENABLED` / `MODEL_SERVING_CANARY_ENABLED` (both default OFF), `test_model_serving.py` (37). **Still open:** (a) apply 024 (`scripts/migrate.sh 024`) and run the Docker suite — the `core_agent.py` hook is verified by `py_compile` + a structural test only; (b) re-point `model_canary_service._STAGE_ROWS` to audited served rows once a canary model has any; (c) add `python -m app.language.model_serving --purge` to cron (line added to `deploy/crontab.example`); (d) calibrate `SERVE_MIN_CONFIDENCE` / `MIN_PER_INTENT_F1` / `AUDIT_SAMPLE_RATE` from audited rows — never lower them to make a model serve.

### A5 (Phase 6, blocked on REAL DATA). Train and evaluate on a real tenant
Apply migration 020 (`scripts/migrate.sh 020`), then
`python -m app.language.train_intent_model --tenant N --dry-run`. Needs
≥60 verified training rows and ≥2 classes with ≥12 rows each, plus an
embedding key. Read `failures`; do not lower thresholds to pass. Done when
one real run is logged here with its report.

### C9. Triage verdicts are not consumed  ⬜ (design decision, not a bug)
`PATCH /admin/novelty-events/{id}` records verdicts; nothing learns from
them. Decide deliberately whether `new_intent`/`existing_intent` verdicts
should feed cluster building or eval-set additions before wiring anything.

## D0. Bug found and fixed while verifying (record for the log)
Four tests in `test_promotion_service.py` were silently broken by the
previous session's eval-set expansion (all 10 intents covered): they
used NEGOTIATION / CREATE_ORDER / GENERAL_QUESTION as "intents with no
eval coverage", so the generalization gate ran the real eval and held
promotion at `pending`. They now use a made-up `UNCOVERED_INTENT`, with
a guard test that fails if cases for it are ever added. **Lesson:** the
"suite passes" claims in the docs were not re-verified after that change.

## D. Known doc/code risks to re-check when touching this area
- Docs drifted from code twice in Phase 5 (threshold names; a
  "⬜ next" for finished work). `tests/test_glb.py` now guards the
  capability→module table; nothing guards the prose. Re-verify status
  rows against code when editing them.
