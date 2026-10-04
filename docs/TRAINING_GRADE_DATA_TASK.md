# docs/TRAINING_GRADE_DATA_TASK.md — governing task spec (verbatim, 2026-09-29)

This is the literal task directive the product owner gave for evolving
Language Intelligence into a training-grade, data-efficient semantic
learning foundation. It **governs** `PHASE_5_8_PLAN.md`,
`OWN_LANGUAGE_MODEL_TRAINING_PLAN.md` and
`EXPERIENCE_DEDUPLICATION_AND_RETENTION.md` — where those documents
conflict with the principles below, THIS document wins, and the others
should be read as *how* these principles are being carried out, not as
a competing plan. Do not redesign the systems named below from scratch;
audit, preserve, extend minimally.

The three deliverables this task requires before coding —
**Current State Matrix**, **Minimal Change Plan**, **Final Gap Report**
— live in `docs/TRAINING_GRADE_DATA_AUDIT.md` (companion file, built
2026-09-29, updated every session). This file is the spec being
audited against; that file is the audit.

---

## Objective

Audit the current Language Intelligence implementation and evolve it
into a **training-grade, data-efficient semantic learning foundation**
without unnecessarily increasing system complexity.

Do NOT redesign the existing systems from scratch (General Language
Brain, 9 language capability depth layers, language understanding,
semantic normalization, surface variation handling, experience
collection, deduplication/retention, cluster building, Shadow Brain,
distillation dataset, generalization evaluation, own-model training /
handover planning).

The goal: make the existing implementation capable of collecting
**high-quality, diverse, semantically useful learning data** that can
eventually support both (1) stronger language understanding and
(2) reliable response generation.

## Core principle

**Data quantity is NOT the primary objective.** Optimize for
**meaningful diversity + semantic quality + verified usefulness**, not
row count.

```text
Exact duplicate           → collapse
Meaning-preserving variation → retain
Near-duplicate             → do NOT blindly delete; retain bounded diverse samples
Large repetitive traffic   → bounded retention / reservoir sampling
```

## 9 Language Capability Layers (preserve the design; not 9 services)

L1 Surface variation · L2 Script & transliteration · L3 Cross-language
equivalence · L4 Code-mixing · L5 Context-dependent meaning · L6
Pragmatic/implied goal · L7 Negation/conditions/quantity · L8
Entities & references · L9 Response capability.

These are capability/evaluation dimensions, not architectural
components. The existing Language Engine / Kernel / Intent / Context /
Memory / RAG boundaries must remain intact.

## Training-grade conceptual record

Where available, a training example should be constructible from:
`surface_input, language, script, transliteration, code_mixing,
normalized_meaning, context_reference, entity_information, intent/goal,
conditions/constraints, knowledge_or_rag_references, teacher_reply,
reply_language, verification_level, teacher_model_version,
quality/outcome, uncertainty`. This does **not** mean one giant table —
reuse existing tables/services; add a field/table only on a real
data-integrity or query need.

## Critical semantic boundary

`normalized_message` is **not** the complete semantic representation:

```text
surface form ≠ normalized text ≠ language-independent meaning ≠ intent
```

Do not hardcode large language-specific phrase maps. Use the existing
LLM/semantic understanding capabilities; let the system learn
business-specific meaning from experience, context, knowledge and
verified outcomes.

## Response learning (capture only — no new architecture yet)

Preserve the ability to associate, when the pipeline already produces a
teacher/LLM response: `teacher_reply, reply_language, knowledge/RAG
used, context used, verification/quality, outcome, teacher
model/version`. Do NOT build a separate response-generation
architecture in this task; make the pipeline capture enough evidence
for future response training.

## Teacher → student strategy (preserve, do not deploy)

```text
External LLM / Teacher → Language understanding → semantic/intent/entity/context
signals → Verified experience data → Dataset builder → Evaluation → Own model
```

Do NOT train or deploy an own generative model as part of this task.
Future handover follows the existing R4 (retrieval/approved responder
for low-risk repetitive intents) → R5 (broader own generative model)
strategy; do not implement R5 prematurely.

## Data quality & diversity

Exact duplicates collapse safely. Meaningful variation is retained.
Near-duplicates are never blindly merged — use bounded
retention/reservoir sampling. Preserve diversity across language,
script, surface form, semantic cluster, intent, code-mixing, contextual
pattern, entity pattern, difficulty. Prefer incremental extension of
existing retention logic over a new multidimensional sampling
framework.

## Quality levels

Preserve or introduce a lightweight verification concept (e.g.
`observed / teacher_generated / verified / approved / rejected`) —
naming matters less than preserving the distinction. Only sufficiently
trusted examples enter high-confidence training datasets.

## Missing training signals to audit (minimum changes only)

context/state used, knowledge/RAG references used, teacher reply, reply
language, teacher uncertainty/confidence, verification/quality, outcome,
entity information, semantic/generalization evaluation examples. For
each: reuse if it exists, extend if incomplete, add the smallest
representation only if required.

## Architecture constraints — strictly preserve, do NOT introduce

Node.js = SaaS backbone · Python = AI service · Kernel = logical layer
inside the Python AI service · existing RAG architecture · PostgreSQL =
source of truth · pgvector · Redis = cache only, not durable source of
truth. Do NOT create a new Kernel/Language/Training microservice,
duplicate RAG, introduce another database, replace PostgreSQL, replace
the Language Engine, rebuild Core Agent, add unnecessary event
infrastructure, or introduce a new ML framework the project doesn't
already need.

## Multi-tenant requirements

Every persistent learning/experience record stays tenant-safe
(`tenant_id`, `customer_id`, `conversation_id`, `channel`, a trace
reference). Never let one tenant's data become another tenant's
training signal. Separate **global language learning** (general
linguistic patterns safe to generalize) from **tenant-specific
knowledge** (products, prices, policies, business rules, customer
data) — the latter must never accidentally become global training data.

## Raw data preservation

Never overwrite the original customer message. Normalized form,
transliteration, semantic representation are all derived data, kept
alongside the raw original, so future reprocessing is possible when the
Language Brain improves.

## Training dataset construction

Production runtime is never responsible for constructing final training
datasets: `Runtime experience → structured durable experience data →
Dataset Builder → filtered/verified examples → training dataset`. The
Dataset Builder selects by quality, verification, language, semantic
cluster, intent, difficulty, diversity, tenant/global scope, teacher
version — implement only the filtering dimensions the existing
architecture supports cleanly.

## Evaluation

Do not rely only on intent accuracy. Extend evaluation to test surface
variation, language equivalence, transliteration, code-mixing, context,
pragmatic meaning, negation, conditions, entities, semantic
generalization, response quality — a compact representative set per
capability layer, not hundreds of tests at once. Goal: detect
regression and prove semantic generalization.

## Implementation strategy

`AUDIT → PRESERVE → MINIMAL EXTENSION → TEST → VERIFY → DOCUMENT`.
Before modifying code: inspect current implementation, map existing
fields/tables/services, identify what's already implemented, identify
actual gaps, avoid duplicating existing concepts. Then implement the
smallest coherent changes.

## Required deliverables (see `TRAINING_GRADE_DATA_AUDIT.md`)

1. Current State Matrix (capability/requirement → existing/partial/
   missing/evidence).
2. Minimal Change Plan (per gap: what/why/where/how/risk; explicitly
   name what should NOT be implemented yet).
3. Implementation (only after 1–2 are clear; preserve all existing
   working behavior).
4. Tests (meaningful surface variation, exact-duplicate handling,
   near-duplicate retention, semantic normalization, context references,
   multilingual/code-mixed cases, training record construction, tenant
   isolation, quality/verification filtering).
5. Final Gap Report — IMPLEMENTED / PARTIAL / DOCUMENTED ONLY / FUTURE,
   never claiming something is implemented merely because a document
   describes it.

## Most important rule

Do NOT turn this into a large "AI training platform" project. This is
still the existing AI Support SaaS; the addition is a **training-grade
learning foundation** on top of the existing Language Intelligence
system. The architecture stays simple enough to operate now, structured
enough to learn later, extensible enough for an own model. Immediate
success is **not** "build our own language model" — it is "every
valuable interaction can become a high-quality, diverse, traceable,
tenant-safe learning example without changing the existing runtime
architecture unnecessarily." Implement incrementally. Do not rewrite
the project. Do not invent missing architecture. Inspect first, extend
only where evidence shows a real gap.
