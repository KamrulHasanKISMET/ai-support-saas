# docs/EXPERIENCE_DEDUPLICATION_AND_RETENTION.md — one experience, one row

Purpose: settle **where the "same vs different" line sits** for
`language_experiences` (the only table that holds raw customer text and
has no retention policy today), so growth stays bounded and privacy
improves WITHOUT throwing away the variety the own model needs to learn
deep language (`OWN_LANGUAGE_MODEL_TRAINING_PLAN.md` §2). **Documentation
only — no code in this doc.** Implementation tasks are listed at the end
for a future session.

Last updated: 2026-09-29 (session 4: P6D-5, P6D-8, P6D-4-schema built — see below). Status: 🟨 design decided, foundation built, write path not yet built.

> Governed by `docs/TRAINING_GRADE_DATA_TASK.md`. Current build status
> and next steps: `docs/TRAINING_GRADE_DATA_AUDIT.md` §4-5.

---

## 1. The tension, stated precisely

Two things are both true and pull in opposite directions:

1. **Growth/privacy:** `language_experiences` has no retention policy
   (`db/init/010_language_experience.sql`, flagged gap) and today writes
   one row per turn with no deduplication. Ten thousand customers asking
   "price koto?" in slightly different words produces ten thousand rows,
   forever.
2. **Learning:** the own model needs **variety** — many different
   surface forms of the same meaning (L1-L4,
   `OWN_LANGUAGE_MODEL_TRAINING_PLAN.md` §2) — and it needs to see
   **minimal pairs** where changing one word changes the meaning (L7
   negation/conditions, `eval_sets/phenomena_v1.json`). Anything that
   collapses rows too eagerly can silently delete exactly the signal
   Phase 6 depends on, and — worse — this kind of data loss is nearly
   invisible: the *other* variant of a pair simply never gets its own
   row, so nobody notices the gap until the model fails on it.

The job is to find a rule that shrinks the *repeat* case hard while
never touching the *variety/minimal-pair* case. Those are treated as
different problems below, not solved with one "similarity score".

## 2. What must NEVER decide "same" — and why (this is the important part)

**Rule: no similarity threshold decides deduplication.** Not edit
distance, not embedding cosine similarity, not any other fuzzy score.

This is a stronger rule than it may sound, so here is the concrete
failure it prevents. Take the L7 minimal pair already in
`eval_sets/phenomena_v1.json`:

- `"I want the red one"` → **CREATE_ORDER**
- `"I do NOT want the red one, do you have other colors?"` → **PRODUCT_INQUIRY**

These two sentences are extremely close by *any* generic similarity
measure — edit distance is small (a handful of tokens), and their
embeddings are close too (embeddings are notoriously weak at negation,
which is exactly why L7 exists as a stress test in the first place). A
dedup rule based on "close enough → merge" would treat the second
sentence as a near-duplicate of the first and either (a) fold it into
the same stored row, discarding the fact that it means the opposite
thing, or (b) never insert it at all because something "close enough"
already exists. Either way, the exact training example the model most
needs — a pair that forces it to notice negation rather than pattern-
match on shared words — is the one a naive dedup rule is most likely to
throw away. The same argument applies to conditionals ("only if it's
red") and to entity changes that matter (different order IDs, different
quantities). **The riskiest inputs for a similarity-based filter are
exactly the riskiest inputs for the model to get wrong — that is not a
coincidence, both come from the same property (small textual/semantic
distance, large meaning distance).**

Note this is a *different* job from the fuzzy/embedding matching the
system already does elsewhere (`cluster_builder_service.py`'s intent
clusters). That matching is fine to be fuzzy because a false "this
looks similar enough" there only means "ask the LLM instead of the own
model" — reversible, cheap, caught by the existing verification chain.
A false "this looks similar enough" in **storage-level dedup** is not
reversible in the same way: the row that would have taught the
difference is simply never written, and nothing downstream can tell
that a gap exists. **The bar for merging two rows must therefore be much
stricter than the bar for routing two messages the same way.**

## 3. What is safe to collapse: exact structural repeats

Two rows may be treated as **the same experience** only when they agree
on every field that carries meaning, using **exact equality**, never a
score:

1. **Same tenant** (never cross-tenant, per §4 of the GLB doc).
2. **Same confirmed intent** (the assigned label, not raw text) — this
   alone already keeps the L7 pairs above apart, since they have
   different intents by construction.
3. **Same language tag.**
4. **Same normalized text after light, lossless normalization only:**
   case-fold, collapse repeated whitespace. **Not** stemming, **not**
   spelling correction, **not** removing punctuation that carries
   meaning (a question mark, or the word "not", is never stripped).
5. **Same entity pattern, not same entity values.** Before comparing
   text, known entity spans (`entity_spans`, already captured) are
   replaced with a typed placeholder — `order #A1042` and
   `order #B9981` both normalize to `order #<ORDER_ID>` — so the *shape*
   of the message counts as the same experience while the *specific*
   order id is not treated as a meaningful difference. The literal text
   of one real example is still kept (§4) so the actual entity values
   are not lost, just not used as a dedup key.

Only when **all five** match exactly is a new turn folded into an
existing row (its `observed_count` increments, `last_seen_at` updates,
verification level only ever upgrades — §2 of the GLB doc already
defines that ordering, reused unchanged here). A typo, a different word
order, a different politeness marker, code-mixing one language turn but
not another — any of these keeps the rows separate, on purpose: that
variety is L1/L4 training signal, not noise to be merged away.

## 4. What bounds growth from *near*-duplicates without merging meaning

Exact-key collapse (§3) only catches literal repeats. It will not, by
itself, stop growth from the much larger pool of near-identical
paraphrases and typo variants ("price koto?", "price koto??", "PRICE
KOTO", "prc koto") — each is a *distinct* exact key, each legitimately
useful variety, and there can be a very large number of them. Merging
them by similarity is exactly what §2 forbids. The answer is **not to
merge them, but to cap how many raw-text examples are kept per
canonical group**, using **reservoir sampling** (a standard, well-
understood technique) rather than similarity judgment:

- A **canonical group** = (tenant, intent, language, entity-normalized
  shape) — the same coarse, exact key from §3, one level up (grouping,
  not merging: every exact-duplicate row still exists inside its group).
- Each group keeps at most **M raw-text samples** (M is a config number,
  a starting point like 50, **not calibrated** — a placeholder like
  every other threshold in this project until real data justifies a
  number).
- Once a group has M samples, a new turn in that group does **not** add
  a new raw-text row. Instead: (a) a lightweight counter for the group
  increments (so frequency information is never lost — this is exactly
  the "how often does this pattern occur" signal calibration/priority
  work needs, GLB §2.3), and (b) with probability M/(count seen so far)
  the new turn **replaces** a uniformly-random existing sample in the
  group (classic reservoir sampling: this keeps the M stored examples a
  fair random cross-section of everything ever seen in that group, not
  just whichever M happened to arrive first — early traffic does not
  permanently freeze what "typical" phrasing looks like for a group).
- Verification level still only upgrades, never downgrades, even when a
  sample is replaced: if the row being evicted was `human_confirmed`,
  eviction is skipped for that slot (a verified example is worth more
  than an arbitrary unverified one) — the reservoir is over the
  *unverified* pool; verified examples are excluded from random eviction
  entirely.

This keeps storage roughly proportional to **the number of distinct
patterns × M**, not to **raw traffic volume**, while (a) never
discarding a minimal pair (different intent ⇒ different group, always
kept), (b) never discarding a distinct entity shape's first sighting,
and (c) never losing the frequency count even after sampling kicks in.

## 5. How this changes the retention picture (from the earlier gap)

The open question from the previous discussion — "how long should
`language_experiences` keep raw text?" — gets easier once §3-4 are in
place, because the thing being retained is no longer "every raw turn
forever" but "a small, capped, frequency-annotated sample per pattern,
plus a count". Two retention layers, not one:

- **The counters and canonical shapes** (no free text: intent, language,
  entity-normalized pattern hash, observed_count, verification_level,
  first/last seen) can reasonably be kept **indefinitely** — this is the
  same kind of thing `turn_understandings` already keeps forever-adjacent
  (well, 90 days, but for the same *reason* — it's metadata, not text)
  and it is exactly what calibration, drift detection (P8-2) and
  priority decisions (§ REAL_TRAFFIC_DATA_COLLECTION.md) need.
- **The up-to-M raw-text samples per group** are the only actual customer
  sentences being stored, and only a bounded number of them. This is
  small enough that a real retention period (a number of days, decided
  by the owner, the same kind of decision as `MODEL_SHADOW_ENABLED` or
  P6R-D1) becomes practical without starving future review or training:
  even if raw text is purged after N days, **the counters survive**, so
  "how much of this pattern have we seen" is never lost, only "the
  literal sentences" age out. A human-confirmed/corrected sample can be
  exempted from this purge specifically, since it is the scarce,
  valuable evidence the whole verification chain exists to produce.

This does not itself pick N — that is still an owner decision, now a
much lower-stakes one because the volume it applies to is capped.

## 6. What this changes for training quality, not just storage

A secondary benefit worth stating: today, `distillation_dataset.py`
already de-duplicates **at training time** (case/whitespace-insensitive,
per §3 of that module) precisely because un-deduplicated storage lets
one over-represented phrasing dominate a training batch — the model
would spend its capacity re-learning the one exact sentence 5,000
customers happened to type, instead of learning the underlying pattern
from a smaller, more diverse sample. Moving deduplication to the storage
layer (§3-4) does not remove the training-time duplicate guard (it
should stay, as a second line of defense — cheap and harmless if
storage-level dedup already did its job), but it does mean the dataset
builder pulls from an already-diverse pool instead of down-sampling a
mostly-repeated one, which should improve both training speed and the
model's exposure to genuine variety per unit of data pulled.

## 7. Decisions still needed from the product owner

| ID | Decision | Default if undecided |
|---|---|---|
| P6D-1 | Reservoir cap **M** per canonical group | 50 (placeholder, uncalibrated) |
| P6D-2 | Raw-text retention period **N** (days) for the capped samples, once built | none set — today's "keep forever" gap persists until this is answered |
| P6D-3 | Should entity-normalization (§3.5) redact/replace the entity value in the ONE kept literal sample too, or only in the dedup key? (Affects whether a stored sample can contain a real order id / phone number at all) | keep the real value in the literal sample (needed for entity-model training, P6T-4), key-only redaction |

## 8. Implementation tasks (for a future coding session — not done yet)

| ID | Task | Depends on |
|---|---|---|
| P6D-4 | Migration: `experience_groups` table | ✅ schema built 2026-09-29 (`db/init/023_experience_groups.sql`), **empty by design, nothing writes to it yet** — waits on P6D-1 |
| P6D-5 | Entity-normalization function: text + `entity_spans` → shape hash | ✅ built 2026-09-29 (`app/language/experience_shape.py`, 19 tests). Honest limitation stated in its docstring: `entity_spans` is untyped today, so the placeholder is untyped too — revisit when P6T-4 adds typed entities |
| P6D-6 | Reservoir-sampling insert path in the write path that today does a plain `INSERT` into `language_experiences` (isolated, same additive/try-except pattern as `record_turn_understanding`, must never block a reply on failure) | P6D-4, P6D-5 |
| P6D-7 | Time-based purge of raw-text samples only (counters survive), human-verified samples exempted | P6D-2, P6D-4 |
| P6D-8 | Backfill/report: run shape-hash grouping over existing `language_experiences` once | ✅ built 2026-09-29 (`app/language/experience_backfill_report.py`, 10 tests on the pure grouping logic). **Not yet run against real data** — needs Docker + real traffic, first blocking step for P6D-1/P6D-2 |

Order: **P6D-5 → P6D-8 (measure first, on real data, before committing a
cap) → P6D-1/P6D-2/P6D-3 (owner decides, now informed by real numbers)
→ P6D-4 → P6D-6 → P6D-7.** Updated 2026-09-29: P6D-5, P6D-8 (code) and
P6D-4 (schema only) are built. **Next real step is running P6D-8 against
Docker/real traffic**, then the owner decisions, then P6D-6/P6D-7.

## 9. One-paragraph summary

Deduplicate on **exact, structural** agreement only (tenant + intent +
language + entity-normalized text) — never on a similarity score,
because the cases a similarity score would most confidently merge
(minimal pairs like negation) are exactly the cases most valuable to
keep separate. Bound growth from genuine near-duplicate variety with a
**capped, randomly-sampled reservoir per canonical group** plus an
always-kept **frequency counter**, not by discarding examples based on
how similar they look. This shrinks the corpus enough that a real
retention period for raw text becomes practical without losing the
signal (frequency, verification) that calibration and training actually
need.
