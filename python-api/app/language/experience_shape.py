"""
EXPERIENCE SHAPE HASH -- entity-normalized canonical form (P6D-5,
docs/EXPERIENCE_DEDUPLICATION_AND_RETENTION.md §3.5,
docs/TRAINING_GRADE_DATA_TASK.md "Data quality & diversity").

Pure, read-only building block: turns a normalized message + its
extracted entity spans into a **shape** that ignores the specific
entity VALUES but keeps everything else exact. Two messages that only
differ in an order id, a quantity, or a product name collapse to the
same shape; two messages that differ in ANY other word (including a
single negation like "not") do NOT.

    "where is order #A1042?"  -> "where is order <e>?"
    "where is order #B9981?"  -> "where is order <e>?"   (same shape)
    "I want the red one"      -> "i want the red one"
    "I do NOT want the red one" -> "i do not want the red one"  (DIFFERENT shape)

This module does not decide what counts as a duplicate on its own — it
only produces the *shape*; a caller combines it with tenant + intent +
language before treating two rows as "the same experience"
(EXPERIENCE_DEDUPLICATION_AND_RETENTION.md §3: same tenant + same
confirmed intent + same language + same shape, ALL required).

Honest limitation, stated because it affects correctness: today's
`entity_spans` (app/language/language_engine.py) is a flat list of raw
substrings from the LLM, not typed (no `type`/`value` split yet --
that's P6T-4, not built). This module therefore produces an UNTYPED
placeholder (`<e>`) for every span, rather than a typed one like
`<ORDER_ID>` vs `<QUANTITY>`. Two spans of genuinely different KINDS
(an order id and a product name) both become `<e>` and so look like
"the same kind of gap" to the shape hash even though they are not --
this is a coarser shape than the design doc's ideal, on purpose (the
smallest change that is honestly available with today's data), and it
is documented here so nobody mistakes `<e>` for a typed slot later. If
P6T-4 adds typed entities, this function should be revisited to emit
typed placeholders using the same input shape (a list of (type, value)
tuples) -- the call sites below would not need to change beyond that.

Every function here is pure, deterministic and total (never raises on
odd input -- a canonicalization helper is not the place to fail a
customer-facing write path).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

PLACEHOLDER = "<e>"
_WS = re.compile(r"\s+")


def _safe_str(x) -> str:
    return x if isinstance(x, str) else ("" if x is None else str(x))


def canonical_form(normalized_message: str | None, entity_spans: list[str] | None) -> str:
    """normalized_message with every entity span replaced by PLACEHOLDER,
    casefolded, whitespace-collapsed. Longer spans are replaced first so
    a span that is a substring of another span doesn't get double-cut."""
    text = _safe_str(normalized_message)
    spans = sorted(
        {_safe_str(s).strip() for s in (entity_spans or []) if _safe_str(s).strip()},
        key=len, reverse=True,
    )
    for span in spans:
        if span:
            text = text.replace(span, PLACEHOLDER)
    return _WS.sub(" ", text).strip().casefold()


def shape_hash(normalized_message: str | None, entity_spans: list[str] | None) -> str:
    """Stable content hash of canonical_form(). Used as (part of) a
    dedup/grouping key -- never as a training feature itself, and never
    reversible back to text (that's what canonical_form is for)."""
    return hashlib.sha256(canonical_form(normalized_message, entity_spans).encode()).hexdigest()


@dataclass(frozen=True)
class GroupKey:
    """The full "same experience" key -- shape_hash ALONE is never
    enough (EXPERIENCE_DEDUPLICATION_AND_RETENTION.md §3): tenant,
    intent and language must all match too, or a shape match across a
    tenant boundary or across two different confirmed intents would
    silently merge things that must stay separate (the exact negation
    failure mode the design doc warns about -- two rows that share a
    shape but were CONFIRMED to different intents must never collapse)."""
    tenant_id: int
    intent: str | None
    language: str
    shape: str

    def as_key(self) -> str:
        return f"{self.tenant_id}:{self.intent or ''}:{self.language}:{self.shape}"


def group_key(*, tenant_id: int, intent: str | None, language: str | None,
              normalized_message: str | None, entity_spans: list[str] | None) -> GroupKey:
    return GroupKey(
        tenant_id=tenant_id,
        intent=intent,
        language=_safe_str(language) or "und",
        shape=shape_hash(normalized_message, entity_spans),
    )
