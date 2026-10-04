"""
GENERAL LANGUAGE BRAIN -- capability A orchestration, first real step
(Phase 5 remainder; PENDING_WORK.md C1, GENERAL_LANGUAGE_BRAIN.md §1.1/§7.1).

`glb.py` gathers what the system understood this turn (TurnUnderstanding).
This module CONSUMES that record and produces a TurnPlan: one place that
answers "given what we understood, what strategy fits this turn?"

    plan_turn(TurnUnderstanding | dict) -> TurnPlan

ADVISORY ONLY (deliberate). The plan is recorded in the response
metadata (`metadata.plan`) so it can be observed and compared with what
actually happened BEFORE anything is allowed to act on it. `enacted` is
always False and a test pins that. Nothing in the Kernel, RoutingService
or reply path reads the plan. Promoting it to a decision-maker is a
separate, explicit change (Phase 7, adaptive routing).

Rules are deliberately few, explicit and uncalibrated (thresholds are
placeholders inherited from glb.py). They encode only what the docs
already state:

  * automation_eligible=False  -> human review before any autonomous
    action (§3; permanent for CREATE_ORDER/ORDER_STATUS).
  * ambiguous                  -> clarify before acting (§5.4).
  * novel or low_confidence    -> the LLM teacher stays in the loop; the
    own model must not answer alone (§6 stage 3-4).
  * language_learning_eligible -> may be recorded as learning material.
    Recording is NOT trust: verification levels (§2) still gate learning.

HARD BOUNDARY (§5.3): the plan is derived only from the understanding
record, which has no person-inference fields. Language/script never
change WHO the customer is assumed to be -- only how the text is handled.

Pure module: stdlib + glb only. No DB, no model, never raises.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from app.language.glb import TurnUnderstanding

# Phenomena that keep the LLM teacher in the loop for this turn.
TEACHER_REQUIRED_PHENOMENA: frozenset[str] = frozenset({"novel", "low_confidence", "ambiguous"})


@dataclass(frozen=True)
class TurnPlan:
    understanding_source: str          # 'own_model_ok' | 'llm_teacher_required'
    automation: str                    # 'allowed' | 'human_review_required'
    clarify_before_acting: bool
    flag_for_triage: bool              # novel -> operator triage queue candidate
    record_as_learning_material: bool  # NOT the same as trusted (see §2)
    reply_language: str | None
    reasons: tuple[str, ...]
    advisory: bool = True
    enacted: bool = False              # invariant: nothing acts on the plan yet


def _get(u: TurnUnderstanding | Mapping[str, Any], name: str, default: Any = None) -> Any:
    if isinstance(u, Mapping):
        return u.get(name, default)
    return getattr(u, name, default)


def plan_turn(u: TurnUnderstanding | Mapping[str, Any]) -> TurnPlan:
    """Pure and total: odd/missing input degrades to the SAFE plan
    (teacher required, human review required), never raises."""
    try:
        phenomena = set(_get(u, "phenomena", ()) or ())
        reasons: list[str] = []

        teacher = bool(phenomena & TEACHER_REQUIRED_PHENOMENA)
        if teacher:
            reasons.append("teacher_required:" + ",".join(sorted(phenomena & TEACHER_REQUIRED_PHENOMENA)))
        if _get(u, "intent") is None:
            teacher = True
            reasons.append("teacher_required:no_intent")

        automation_ok = bool(_get(u, "automation_eligible", False))
        if not automation_ok:
            reasons.append("human_review:automation_ineligible_intent")

        ambiguous = "ambiguous" in phenomena
        if ambiguous:
            reasons.append("clarify:ambiguous")

        novel = "novel" in phenomena
        if novel:
            reasons.append("triage:novel")

        learn = bool(_get(u, "language_learning_eligible", False))
        if not learn:
            reasons.append("no_learning:ineligible")

        return TurnPlan(
            understanding_source="llm_teacher_required" if teacher else "own_model_ok",
            automation="allowed" if automation_ok else "human_review_required",
            clarify_before_acting=ambiguous,
            flag_for_triage=novel,
            record_as_learning_material=learn,
            reply_language=_get(u, "reply_language"),
            reasons=tuple(reasons),
        )
    except Exception:  # noqa: BLE001 -- total function by contract
        return TurnPlan(
            understanding_source="llm_teacher_required",
            automation="human_review_required",
            clarify_before_acting=False,
            flag_for_triage=False,
            record_as_learning_material=False,
            reply_language=None,
            reasons=("plan_failed_safe_default",),
        )


def plan_to_dict(p: TurnPlan) -> dict:
    d = asdict(p)
    d["reasons"] = list(p.reasons)
    return d
