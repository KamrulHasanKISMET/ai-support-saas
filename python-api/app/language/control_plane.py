"""
LANGUAGE INTELLIGENCE — CONTROL PLANE (Phase 4 corrected scope)

docs/GENERAL_LANGUAGE_BRAIN.md §3 / §3.1 / §9 item 1. This is the
single source of truth for a distinction the repo used to collapse
into one bit:

    Language understanding and business-action authority are separate
    control planes. A message can be fully, confidently understood and
    still be forbidden from triggering autonomous action.

Before this module existed, one frozenset was copy-pasted across three
files under three different names -- core_agent.py's
`_LEARNING_INELIGIBLE_INTENTS`, routing_service.py's
`ROUTING_INELIGIBLE_INTENTS`, and kernel.py's `INTENT_HINT_INELIGIBLE`
-- all containing the same two intents (CREATE_ORDER, ORDER_STATUS)
for what were actually two different questions ("don't train on
these" vs "don't route these to the brain"). That conflation is
exactly what GENERAL_LANGUAGE_BRAIN.md Correction #3 forbids: a
message like "amar parcel ta koi?" ("where is my order?") is excellent
language-learning material and simultaneously unsafe to auto-resolve
-- a single flag cannot express that.

Four independent flags, evaluated per intent (and, per §3,
overridable per tenant the same way `tenant_calibration_config`
already overrides calibration constants -- no per-tenant override
table exists yet because nothing has needed one so far; add one here,
not a fifth copy-pasted set, if that becomes necessary):

  - language_learning_eligible: may this message's *language pattern*
    (surface -> semantic mapping) be learned from?
  - automation_eligible: may the system act on this intent *without a
    human in the loop*, once confidently understood?
  - promotion_eligible: may a candidate cluster built from this
    intent's experiences be promoted into the live `intent_clusters`
    set (i.e. affect routing) via PromotionService?
  - training_eligible: may this experience be used in an offline
    training/distillation dataset for a future own model?

Worked example (the one in the correction):

    "amar parcel ta koi?" (ORDER_STATUS)
      language_learning_eligible = TRUE  -- yes, learn the phrasing
      automation_eligible        = FALSE -- no autonomous action
      promotion_eligible         = TRUE  -- routing may still use the
                                            learned pattern to route
                                            *understanding* fast
      training_eligible          = TRUE  -- usable in a future
                                            distilled model's training
                                            set (subject to §4's
                                            tenant-isolation rules)

Today only `automation_eligible` excludes anything
(AUTOMATION_INELIGIBLE_INTENTS below). This module does not itself
gate the not-yet-built Tool Engine -- ROADMAP.md §3 marks that
insertion point in kernel.py (the
`if intent_result.intent in (CREATE_ORDER, ORDER_STATUS)` block).
What this module guarantees is that whenever the Tool Engine is built,
there is exactly one place to ask "is this intent automation_eligible?"
-- and that the language layer (routing, learning, promotion,
training) has already stopped conflating that question with its own.
"""

from app.intent.intent_types import IntentType

# The ONLY flag that excludes anything today. CREATE_ORDER / ORDER_STATUS
# touch real money/fulfillment and must stay LLM+human-reviewed for
# autonomous ACTION, regardless of how confidently the language layer
# understands them (GENERAL_LANGUAGE_BRAIN.md §3, permanent per §11 Q25).
# There is no autonomous action to gate yet (no Tool Engine exists --
# ROADMAP.md §3), so nothing calls is_automation_eligible() in anger
# today; it exists now so the Tool Engine has a single, already-correct
# place to ask this question when it is built, instead of reinventing
# (or mis-copying) the exclusion list itself.
AUTOMATION_INELIGIBLE_INTENTS: frozenset[str] = frozenset(
    {IntentType.CREATE_ORDER.value, IntentType.ORDER_STATUS.value}
)

# Corrected scope: nothing is excluded from language learning today.
# CREATE_ORDER/ORDER_STATUS used to be excluded here too (the old
# _LEARNING_INELIGIBLE_INTENTS) -- that was the literal bug Correction
# #3 calls out. "amar parcel ta koi?" is exactly the kind of phrasing
# the Brain should learn, even though it must never autonomously act
# on it (see AUTOMATION_INELIGIBLE_INTENTS above).
LANGUAGE_LEARNING_INELIGIBLE_INTENTS: frozenset[str] = frozenset()

# Corrected scope: nothing is excluded from routing-for-understanding
# either. Recognizing an intent fast (skipping the LLM Intent Engine
# call, or injecting a brain-predicted hint) is NOT the same as
# authorizing an action -- it used to be blocked outright by
# routing_service.py's ROUTING_INELIGIBLE_INTENTS and kernel.py's
# INTENT_HINT_INELIGIBLE, both of which duplicated
# AUTOMATION_INELIGIBLE_INTENTS's contents. That conflation is fixed
# here: a cluster built from CREATE_ORDER/ORDER_STATUS experiences may
# now be promoted into live routing exactly like any other intent's.
PROMOTION_INELIGIBLE_INTENTS: frozenset[str] = frozenset()

# Corrected scope: nothing is excluded from offline training/
# distillation eligibility today either. Subject to the same
# tenant-isolation rules as any other data (§4) -- unrelated to, and
# not enforced by, this per-intent flag.
TRAINING_INELIGIBLE_INTENTS: frozenset[str] = frozenset()


def is_language_learning_eligible(intent: str | None) -> bool:
    """May this message's language pattern (surface -> semantic mapping)
    be learned from? Independent of automation_eligible -- see module
    docstring's worked example."""
    return intent not in LANGUAGE_LEARNING_INELIGIBLE_INTENTS


def is_automation_eligible(intent: str | None) -> bool:
    """May the system act on this intent without a human in the loop,
    once confidently understood? The ONLY flag that excludes anything
    today (CREATE_ORDER, ORDER_STATUS) -- this is a permanent,
    business-risk gate, not a tuning knob."""
    return intent not in AUTOMATION_INELIGIBLE_INTENTS


def is_promotion_eligible(intent: str | None) -> bool:
    """May a candidate cluster built from this intent's experiences be
    promoted into the live intent_clusters set (affect routing)?
    Independent of automation_eligible -- promoting a cluster only
    speeds up *understanding*, it never authorizes action on its own."""
    return intent not in PROMOTION_INELIGIBLE_INTENTS


def is_training_eligible(intent: str | None) -> bool:
    """May this experience be used in a future offline training/
    distillation dataset for an own model?"""
    return intent not in TRAINING_INELIGIBLE_INTENTS
