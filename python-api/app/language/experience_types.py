"""
LANGUAGE EXPERIENCE STORE — types.

One LanguageExperience = one Language Engine call, recorded after
CoreAgent.run() finishes. Mirrors the `language_experiences` table
(db/init/010_language_experience.sql) field for field -- same
write-path-data-carrier pattern as app/trace/trace_types.py's
AgentRunTrace, deliberately: no aggregation/query helpers live here,
this is Phase 0 (schema + logging only).
"""

from dataclasses import dataclass, field
from uuid import UUID


@dataclass
class LanguageExperience:
    experience_id: UUID
    tenant_id: int
    customer_id: int
    conversation_id: int
    message_id: int | None = None
    request_id: str | None = None
    channel: str | None = None

    original_message: str | None = None
    detected_language: str | None = None
    reply_language: str | None = None
    normalized_message: str | None = None
    communication_style: str | None = None
    is_ambiguous: bool = False
    ambiguity_reason: str | None = None
    entity_spans: list[str] = field(default_factory=list)

    # Brain vs. LLM bookkeeping -- see the migration's header comment.
    # brain_used/brain_prediction/brain_version stay at their defaults
    # (no own Brain exists yet); llm_called defaults True (100% of
    # traffic is LLM-driven today).
    brain_used: bool = False
    brain_prediction: dict | None = None
    brain_version: str | None = None
    llm_called: bool = True
    llm_raw_confidence: float | None = None  # Language Engine's own,
    # UNCALIBRATED self-reported confidence -- see migration comment.
    llm_model_version: str | None = None

    final_intent: str | None = None
    verification_result: str = "unverified"  # 'verified_correct' | 'verified_incorrect' | 'unverified'
    # Legacy field (above) -- left in place, unread by any new code, no
    # migration needed to drop it. The evidence hierarchy
    # (docs/GENERAL_LANGUAGE_BRAIN.md §2.2) is carried by the two fields
    # below instead:
    learning_eligible: bool = True
    verification_level: str = "unverified"
    # 'unverified' | 'self_consistent' | 'outcome_positive' |
    # 'outcome_negative' | 'human_confirmed' | 'human_corrected'.
    # Only 'outcome_positive'/'human_confirmed' (or 'self_consistent'
    # with source_reliability >= tenant's min_reliability) make a row
    # an eligible learning signal for ClusterBuilderService (§2.3).
    source_reliability: float | None = None
    # Only meaningful when verification_level='self_consistent': the
    # calibrated agreement_rate of the cluster the brain agreed with,
    # snapshotted at write time (see core_agent.py). None for every
    # other verification_level.
    superseded_by: str | None = None
    # Soft pointer (experience_id, as str) to the row that superseded
    # this one's verification/label -- e.g. a human_confirmed label
    # later human_corrected. None = not superseded. Append-only: the
    # earlier row is never deleted or overwritten (§2.4). Nothing
    # writes this at insert time yet (no label-revision UI exists) --
    # it defaults None and is updated in place only by that future
    # workflow.

    # ── Language-agnostic schema (§5.2 / migration 016) ───────────────
    # Four concepts previously collapsed into `detected_language` alone
    # are now separated. All have safe defaults so existing callers that
    # don't set them get sensible values without any code change.
    script: str = "und"
    # Writing system actually used in original_message. OPEN string
    # (any script: "latin", "bengali", "thai", "hangul", "cyrillic", ...).
    # Default "und" = not reported -- never a silent claim of "latin".
    is_transliterated: bool = False
    # True when message is in Latin script but the underlying language
    # is non-Latin (e.g. Banglish). Default False.
    transliterated_from: str | None = None
    # Best-guess source script when is_transliterated=True. None otherwise.
    code_mixing: str = "none"
    # "none" | "inter_sentential" | "intra_sentential". Default "none".

    # ── Open language tag (migration 017, added after the four above) ──
    # docs/GENERAL_LANGUAGE_BRAIN.md §5.2 row 1 -- the field the earlier
    # increment deliberately deferred (detected_language stayed a closed
    # enum on purpose; see that migration's header). Open BCP-47-style
    # tag ("bn", "en", "hi", ...), comma-separated only when genuinely
    # code-mixed. "und" = undetermined, the safe default for rows
    # written before this field existed and for parse fallback.
    language: str = "und"
