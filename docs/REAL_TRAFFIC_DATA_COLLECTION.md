# docs/REAL_TRAFFIC_DATA_COLLECTION.md — what real traffic must produce, and what to do with it

Items in `PHASE_5_8_PLAN.md` marked 🔒 unblock only when the data below
exists. **Nothing here needs new code to start collecting** except where
marked "switch on". Each row: what is collected → where it lands → the
check that says "enough" → which plan item it unblocks.

Do the one-time setup first: apply migrations (`scripts/migrate.sh 018 019 020 021 021`),
schedule the jobs (`deploy/crontab.example`), then let traffic run.

## 1. Collected automatically once traffic flows

| Data | Table / source | Written by | Retention | Unblocks |
|---|---|---|---|---|
| Every Language-Engine call (text, intent, verification level) | `language_experiences` | `core_agent` (Phase 0) | none set (contains text — set a policy before production). **Also: no deduplication today, one row per turn.** Design for both, without losing minimal-pair/variety signal: `docs/EXPERIENCE_DEDUPLICATION_AND_RETENTION.md` | P6-1, P5-10, P5-8 |
| Brain-vs-LLM agreement per turn | `language_experiences.brain_used`, `routing_decisions.was_correct` | `core_agent` | none | P5-10, calibration, P7-2 |
| Turn understanding (language/script/phenomena/flags) — no text | `turn_understandings` | `core_agent` | 90 d | P5-8, P5-9 priorities, P7-1, P8-2a |
| Novel messages for triage | `novelty_events` | `novelty_detector` | none | P5-6, new intents |
| Cost per conversation | `conversation_costs` | `conversation_cost_tracker` | none | P8-1a |
| Per-step latency, tokens, errors | `agent_run_traces` | `record_trace` | none | P7-1, P8 |
| Which cluster served a turn | `routing_decisions.served_branch` | `core_agent` (needs 019) | none | canary ramp |

## 2. Needs a switch or a human action

| Data | How to start | Unblocks |
|---|---|---|
| **Verified labels** (the scarce resource) | Use `POST /language/experiences/{id}/confirm\|correct` (`verification_service`). `self_consistent` rows appear automatically when brain and LLM agree; humans add `human_confirmed`/`human_corrected`. Prioritise languages with the most traffic from the understanding report. | P6-1 (training needs verified rows) |
| Model shadow predictions | Train (P6-1), move the model to `shadow` (P6-2a), set `MODEL_SHADOW_ENABLED=true`. Costs one extra embedding per turn. | P6-3 |
| Native review of the eval set | `eval_set_review.py export` → reviewers → `apply` | P5-7, P5-9 |
| Prices | Set `MODEL_PRICING_JSON` | P8-1a |

## 3. Decision checkpoints (run these; record the result in `PENDING_WORK.md`)

Run in this order when the stated volume is reached. **Volumes are
starting points, not calibrated facts.**

| When | Run | Read | Decide |
|---|---|---|---|
| ~1 week of traffic | `python -m app.language.understanding_report --tenant N --days 7` | language/script mix, `unresolved`, novel / code-mixed share | which languages to review & label first (C1c) |
| ≥30 brain turns per intent | `calibration_scheduler` (dry-run then real) | thresholds written | first agreement-rate numbers (A4 done) |
| After 017 live + traffic | `python -m app.language.language_tag_readiness --since <date>` | READY / NOT READY | whether to start the `detectedLanguage` removal (P5-8; mind the gate trap) |
| ≥60 verified rows, ≥2 classes ≥12 each | `python -m app.language.train_intent_model --tenant N --dry-run` | `failures`, `accuracy` vs `baseline_accuracy`, `per_language` | whether to register (real run) and whether softmax is enough (P6-5) |
| real traffic already collected | one-off shape-hash grouping report over existing `language_experiences` (P6D-8, not yet built) | how many rows are literal repeats vs genuine variety | calibrates the reservoir cap M (P6D-1) before the dedup write-path is built |
| Model in shadow, ≥200 scored turns | `model_shadow.report_for(db, N, version)` | `unmet_for_canary`, per-language agreement | start a canary (P6-3) or retrain |
| ≥2 real generalization runs | `generalization_eval --suggest` ×2 → `calibration_apply` | thresholds | flip `THRESHOLDS_CALIBRATED` (P5-7) |
| Weeks of `routing_decisions` | drift detectors (P8-2a) + `distribution_shift`/`agreement_drop` | PSI, drop | whether routing needs a learned policy (P7-2) |

## 4. Rules for the data
- Text exists only in `language_experiences` (and `novelty_events`); every
  newer table is metadata-only. Keep it that way; tests enforce it.
- Tenant data trains that tenant's model only (§4). Cross-tenant learning
  is blocked on decision P7-3.
- Agreement with the LLM is **not** ground truth; only outcome/human
  verification is. Do not report agreement as accuracy.
- Record every real run (date, command, summary) in `PENDING_WORK.md`;
  unrecorded runs did not happen.
