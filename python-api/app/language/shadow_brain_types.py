"""
SHADOW (PASSIVE) LANGUAGE BRAIN -- types.

Mirrors the small, single-purpose type-carrier pattern already used by
app/language/experience_types.py and app/trace/trace_types.py: this
file holds only the data shape, no matching/DB logic (that's
app/language/shadow_brain.py).
"""

from dataclasses import dataclass


@dataclass
class BrainPrediction:
    """
    One shadow-mode prediction: the closest known intent cluster for a
    given message, plus a similarity score (docs/LANGUAGE_INTELLIGENCE.md
    Phase 1, step 3 -- "the entire v1 own Brain"). Purely descriptive:
    nothing on this class is ever used to change `reply`, `intent`, or
    any other customer-facing field -- see shadow_brain.py's docstring.
    """

    predicted_intent: str
    similarity: float
    cluster_id: int
    matched_example: str
