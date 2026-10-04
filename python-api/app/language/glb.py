"""
GENERAL LANGUAGE BRAIN -- Phase 5 umbrella (capability A).
docs/GENERAL_LANGUAGE_BRAIN.md §1.1 / §7.1.

Until now "the Brain" meant one thing: the intent matcher (capability B).
This module is the first concrete piece of capability A. It does two
small, honest things and deliberately nothing else:

1. CAPABILITIES -- a machine-readable registry of §1.1's capabilities
   A-M: what each answers, which repo modules implement it, and its
   REAL status. tests/test_glb.py checks every listed module path
   exists, so this table cannot drift from the code the way the prose
   tables did.
2. TurnUnderstanding + assemble_turn_understanding() -- one read-only
   record per turn that gathers what B (intent), C/language layer
   (open tag, script, transliteration, code-mixing), and the control
   plane already computed, into a single structure. It computes NO new
   understanding and calls NO model. It is the seam a future
   orchestrator / distilled model (Phase 6) will read and write.

What this is NOT: it does not route, decide, learn or act. Nothing here
orchestrates B-M yet -- that is a later step, tracked in
docs/PENDING_WORK.md. Pure module: stdlib + control_plane only, so it
is testable without a database.

HARD BOUNDARY (§5.3): language / script / transliteration are data-
quality and evaluation signals only. TurnUnderstanding intentionally
has NO field for nationality, ethnicity, identity, geography or
location, and tests pin that.

Print the capability table:
    python -m app.language.glb
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from pathlib import Path

from app.language import control_plane


class CapabilityStatus(str, Enum):
    BUILT = "built"
    PARTIAL = "partial"                      # exists but incomplete / unpopulated
    FOUNDATION_UNWIRED = "foundation_unwired"  # tested module, no production caller
    IMPLICIT = "implicit"                    # happens inside another component
    NOT_BUILT = "not_built"


@dataclass(frozen=True)
class Capability:
    code: str
    name: str
    answers: str
    modules: tuple[str, ...]   # repo-relative to python-api/; must exist
    status: CapabilityStatus
    note: str = ""


S = CapabilityStatus
CAPABILITIES: tuple[Capability, ...] = (
    Capability("A", "General Language Brain", "orchestrates B-M, owns the evidence hierarchy and learning loop",
               ("app/language/glb.py", "app/language/understanding_store.py",
                "app/language/understanding_report.py", "app/language/glb_orchestrator.py"), S.PARTIAL,
               "registry + per-turn record (persisted, 90-day retention, read by a per-tenant report); advisory plan only (recorded, never enacted)"),
    Capability("B", "Intent understanding", "which structured intent does this serve?",
               ("app/intent/intent_engine.py", "app/language/shadow_brain.py"), S.BUILT,
               "LLM engine + own nearest-example matcher"),
    Capability("C", "Entity understanding", "what specific things are named?",
               ("app/intent/intent_engine.py", "app/language/language_engine.py"), S.BUILT),
    Capability("D", "Context understanding", "what does this mean given the conversation?",
               ("app/context/context_engine.py", "app/state/state_engine.py"), S.PARTIAL,
               "no pronoun/ellipsis resolution; is_ambiguous is flagged, not resolved"),
    Capability("E", "Customer memory", "what do we know about this customer?",
               ("app/memory/memory_service.py", "app/memory/memory_search.py"), S.BUILT),
    Capability("F", "Business knowledge", "what does this tenant's catalog/policy say?",
               ("app/rag/hybrid_search.py", "app/agent/tenant_brain.py"), S.PARTIAL,
               "no ingestion pipeline; business_rules unread by Kernel"),
    Capability("G", "Tenant Brain", "what is this tenant configured to do?",
               ("app/agent/tenant_brain.py",), S.FOUNDATION_UNWIRED),
    Capability("H", "RAG", "retrieval over F",
               ("app/rag/search.py", "app/rag/reranker.py", "app/rag/context_builder.py"), S.PARTIAL,
               "empty corpus"),
    Capability("I", "Reasoning", "combine B-H into a course of action",
               ("app/kernel/kernel.py",), S.IMPLICIT, "inside the single LLM prompt; not a component"),
    Capability("J", "Decision-making", "act, ask, or escalate?",
               ("app/kernel/kernel.py", "app/goal/goal_types.py"), S.PARTIAL,
               "confidence gate only; no Decision Engine"),
    Capability("K", "Tools / actions", "execute a business action",
               ("app/capabilities/capability_registry.py",), S.NOT_BUILT,
               "registry contract exists, empty; automation gated by control_plane"),
    Capability("L", "Response generation", "produce the customer-facing reply",
               ("app/kernel/kernel.py",), S.BUILT),
    Capability("M", "Evaluation / learning", "did B-L do the right thing; update anything?",
               ("app/language/calibration_service.py", "app/language/promotion_service.py",
                "app/language/novelty_detector.py", "app/language/generalization_eval.py",
                "app/language/verification_service.py", "app/language/model_eval.py",
                "app/language/model_registry.py", "app/language/model_shadow.py",
                "app/language/drift_detection.py"), S.PARTIAL,
               "covers B (intent) only; Phase 6 model eval/registry/shadow + Phase 8 drift built, mostly unwired"),
)


def capability(code: str) -> Capability:
    for c in CAPABILITIES:
        if c.code == code:
            return c
    raise KeyError(code)


def missing_modules(python_api_root: Path | None = None) -> list[str]:
    """Registry paths that do not exist on disk (drift detector)."""
    root = python_api_root or Path(__file__).resolve().parents[2]
    return [m for c in CAPABILITIES for m in c.modules if not (root / m).is_file()]


def capability_report() -> str:
    lines = ["GENERAL LANGUAGE BRAIN -- capabilities (docs/GENERAL_LANGUAGE_BRAIN.md §1.1)"]
    for c in CAPABILITIES:
        tail = f"  -- {c.note}" if c.note else ""
        lines.append(f"  {c.code}  {c.name:<24} {c.status.value:<19}{tail}")
    return "\n".join(lines)


# ── per-turn record ─────────────────────────────────────────────────

LOW_CONFIDENCE = 0.6  # placeholder for the "low_confidence" phenomenon flag; uncalibrated


@dataclass(frozen=True)
class TurnUnderstanding:
    """Read-only summary of what the system understood this turn.
    No field here is derived from, or may be used to infer, anything
    about the person (§5.3)."""

    # surface (language layer)
    language: str = "und"             # open tag; "bn,en" when code-mixed
    script: str = "und"
    is_transliterated: bool = False
    code_mixing: str = "none"
    reply_language: str | None = None
    normalized_message: str | None = None
    # meaning (capability B)
    intent: str | None = None
    intent_confidence: float | None = None
    intent_source: str | None = None  # 'brain' | 'llm' | None
    # control plane (independent flags, §3)
    language_learning_eligible: bool = True
    automation_eligible: bool = True
    promotion_eligible: bool = True
    training_eligible: bool = True
    # recurring phenomena worth an own mechanism later (§5.4)
    phenomena: tuple[str, ...] = field(default_factory=tuple)


def assemble_turn_understanding(
    *,
    language: str | None = None,
    script: str | None = None,
    is_transliterated: bool = False,
    code_mixing: str | None = None,
    reply_language: str | None = None,
    normalized_message: str | None = None,
    intent: str | None = None,
    intent_confidence: float | None = None,
    intent_source: str | None = None,
    is_ambiguous: bool = False,
    is_novel: bool = False,
) -> TurnUnderstanding:
    """Pure. Missing/blank inputs become honest defaults ('und', None),
    never guesses. Never raises on odd input."""
    mixing = code_mixing or "none"
    phenomena: list[str] = []
    if mixing != "none":
        phenomena.append("code_mixed")
    if is_transliterated:
        phenomena.append("transliterated")
    if is_ambiguous:
        phenomena.append("ambiguous")
    if is_novel:
        phenomena.append("novel")
    try:
        if intent_confidence is not None and float(intent_confidence) < LOW_CONFIDENCE:
            phenomena.append("low_confidence")
    except (TypeError, ValueError):
        pass
    return TurnUnderstanding(
        language=(language or "und").strip() or "und",
        script=(script or "und").strip() or "und",
        is_transliterated=bool(is_transliterated),
        code_mixing=mixing,
        reply_language=reply_language,
        normalized_message=normalized_message,
        intent=intent,
        intent_confidence=intent_confidence,
        intent_source=intent_source,
        language_learning_eligible=control_plane.is_language_learning_eligible(intent),
        automation_eligible=control_plane.is_automation_eligible(intent),
        promotion_eligible=control_plane.is_promotion_eligible(intent),
        training_eligible=control_plane.is_training_eligible(intent),
        phenomena=tuple(phenomena),
    )


def understanding_to_dict(u: TurnUnderstanding) -> dict:
    d = asdict(u)
    d["phenomena"] = list(u.phenomena)
    return d


FORBIDDEN_FIELD_WORDS = ("nationality", "ethnic", "religion", "country", "location", "geo", "race", "identity")


def field_names() -> list[str]:
    return [f.name for f in fields(TurnUnderstanding)]


if __name__ == "__main__":
    print(capability_report())
    gone = missing_modules()
    print("\nMISSING MODULES:", gone or "none")
    raise SystemExit(1 if gone else 0)
