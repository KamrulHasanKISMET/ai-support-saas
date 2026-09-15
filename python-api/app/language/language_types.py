from dataclasses import dataclass, field


@dataclass
class LanguageResult:
    """
    Output of the Language Engine. Per architecture doc section 14:
    "Language ≠ Locale ≠ Intent ≠ Communication Style ≠ Customer
    Preference" — this deliberately separates several different things
    instead of collapsing them into one guess.

    Core fields (unchanged from the previous version — preserved
    exactly, never overwritten by the ones added below):

    - original_message: the customer's message exactly as received.
      Carried on this object too (not just as a separate variable in
      the Kernel) so any consumer holding a LanguageResult is
      self-contained. This is NEVER overwritten by normalized_message —
      storage, Memory extraction, and what's shown to the LLM as
      "CUSTOMER QUESTION" all keep using this, not the normalized form.
    - detected_language: what script/mix the customer actually wrote in
      (bn / en / mixed / other). Purely observational, not an assumption
      about who they are.
    - reply_language: what language the AI should reply in. Inferred
      from the message itself (never from locale or nationality).
    - normalized_message: a clear, semantic-meaning-preserving rewrite
      used ONLY for internal understanding (Intent classification, RAG
      retrieval) — never shown to the customer, and never replaces the
      original message in storage/memory. This IS the "semantic
      meaning" representation; there is no separate field for it.
    - confidence: the Language Engine's own confidence in the above.

    New structure (this version — additive only, minimum needed so
    Intent/RAG/Context/Kernel can safely consume richer signal without
    Language Engine taking over their jobs):

    - communication_style: coarse tone/register of the message
      ("formal" | "informal" | "urgent" | "polite" | "neutral").
      Logged for observability; not yet consumed to change reply tone
      (that's a future increment, not this one).
    - is_ambiguous / ambiguity_reason: flags messages whose meaning
      depends on missing context — most commonly short follow-ups like
      "oita koto?" ("how much is that?") where "that" has no antecedent
      within the message itself. This is a SIGNAL, not a decision — the
      Kernel's confidence gate is unchanged in this version; a future
      Decision Engine increment can choose to act on it.
    - entity_spans: raw substrings from original_message that look
      entity-like (product names, sizes, quantities, order IDs, dates,
      numbers) — BOUNDARY ONLY, never typed or labeled. Deciding what
      each span actually IS (and turning it into a typed, tenant-scoped
      entity) stays the Intent Engine's job — Language Engine only
      marks candidates. This keeps Language and Entity extraction
      separate responsibilities even though one feeds the other.
    """

    original_message: str
    detected_language: str
    reply_language: str
    normalized_message: str
    confidence: float

    communication_style: str = "neutral"
    is_ambiguous: bool = False
    ambiguity_reason: str | None = None
    entity_spans: list[str] = field(default_factory=list)
