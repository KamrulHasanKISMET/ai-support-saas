"""
GOAL / OUTCOME — architectural foundation only.

Responsibility: define a minimal structure that can LATER connect
customer intent + conversation state + tenant goals, so a future
Decision Engine has a stable place to reason about "what outcome is
this conversation oriented toward" (e.g. complete a purchase, resolve
a complaint) separately from "what did the customer literally say"
(Intent Engine's job) or "what facts are known" (State Engine's job).

This module does NOT override customer intent or business rules --
`infer_goal_signal()` is a pure, side-effect-free function that reads
an IntentType and returns a GoalSignal; nothing consumes its output
yet. It is not called from app/kernel/kernel.py in this version.
"""

from dataclasses import dataclass

from app.intent.intent_types import IntentType


@dataclass
class GoalSignal:
    """
    A candidate business outcome for the current turn -- a SIGNAL, not
    a decision. `goal_type` is intentionally a plain string (not an
    enum) so tenant-specific verticals can introduce their own goal
    types later without a code change here.
    """

    goal_type: str
    source: str  # how this was derived, e.g. "intent_mapping"
    confidence: float


# Static, coarse mapping from Intent -> a default e-commerce-vertical
# goal. This is intentionally simple (a dict lookup) -- a real Goal
# Engine would also weigh State and TenantBrain.agent_config.vertical,
# neither of which this function touches yet.
_DEFAULT_INTENT_TO_GOAL: dict[IntentType, str] = {
    IntentType.ORDER_STATUS: "post_purchase_support",
    IntentType.PRODUCT_INFO: "purchase_consideration",
    IntentType.PRODUCT_AVAILABILITY: "purchase_consideration",
    IntentType.PRICE_INQUIRY: "purchase_consideration",
    IntentType.DELIVERY_INFO: "post_purchase_support",
    IntentType.RETURN_REQUEST: "resolve_complaint",
    IntentType.COMPLAINT: "resolve_complaint",
    IntentType.NEGOTIATION: "purchase_consideration",
    IntentType.CREATE_ORDER: "complete_purchase",
    IntentType.GENERAL_QUESTION: "information_only",
}


def infer_goal_signal(intent: IntentType) -> GoalSignal:
    """
    Pure function: IntentType -> GoalSignal. No I/O, no DB, no LLM
    call -- safe to call from anywhere without side effects. Confidence
    here reflects how directly the mapping table applies, NOT the
    Intent Engine's own classification confidence (those are two
    different things and must not be confused).
    """
    goal_type = _DEFAULT_INTENT_TO_GOAL.get(intent, "information_only")
    return GoalSignal(goal_type=goal_type, source="intent_mapping", confidence=0.6)
