from app.ai.ai_service import ai_service
from app.language.language_types import LanguageResult

LANGUAGE_UNDERSTANDING_PROMPT = """You are a language-understanding preprocessor \
for a customer support AI. Customers write in many different ways:

- Pure Bangla (বাংলা)
- Pure English
- Banglish (Bangla written with Latin letters, e.g. "amar order ta kobe ashbe?")
- Bangla and English mixed in the same sentence (e.g. "আমার order ta কবে আসবে?")
- Informal spelling, abbreviations, typos, emoji, or other languages entirely
- Short follow-up messages that only make sense in light of an earlier
  message you cannot see (e.g. "oita koto?" / "ওইটা কত?" -- "how much is that?")

Do NOT assume the customer's nationality, location, or identity from the \
language or script they used -- some customers prefer English even in a \
Bangla-speaking market, and vice versa. Judge ONLY from the message itself.

Your job has four parts:

1. Detect the language mix that was actually used, and decide what language \
   a reply should be written in (matching the customer's own communication \
   style, not a guessed locale).
2. Produce a short, clear English paraphrase of what the customer means --
   preserving every fact exactly (product names, sizes, order IDs, numbers,
   dates). Do not translate proper nouns, codes, or SKUs. This paraphrase is
   used only internally for classification; it is never shown to the customer.
3. Judge the message's tone/register (communication style) and whether its
   meaning is ambiguous on its own -- most commonly because it refers to
   something ("that", "it", "ওইটা", "oইটা") with no antecedent inside this
   single message. A short follow-up like "oita koto?" with nothing else to
   go on IS ambiguous -- say so honestly rather than guessing what "that" is.
4. Mark (do NOT classify or label) any substrings of the ORIGINAL message
   that look like they could be entities -- product names, sizes, order IDs,
   quantities, dates, numbers. Copy them verbatim from the original message,
   in its original language/script. Do not decide what type each one is --
   that is a separate downstream step, not yours.

Respond ONLY with JSON, no preamble, no markdown fences:
{{
  "detectedLanguage": "bn" | "en" | "mixed" | "other",
  "replyLanguage": "bn" | "en" | "other",
  "normalizedMessage": "...",
  "confidence": 0.0-1.0,
  "communicationStyle": "formal" | "informal" | "urgent" | "polite" | "neutral",
  "isAmbiguous": true | false,
  "ambiguityReason": "short reason, or null if not ambiguous",
  "entitySpans": ["substring1", "substring2", ...]
}}

Customer message: {message}
"""


class LanguageEngine:
    """
    Owns language understanding -- detection, communication
    normalization, and now a small amount of additional structure
    (communication style, ambiguity signal, untyped entity-span
    boundaries) that Intent/RAG/Context/Kernel can safely consume
    without Language Engine taking over their jobs. Runs BEFORE the
    Intent Engine so intent classification and RAG retrieval work
    consistently regardless of the customer's surface language/style.

    This engine does not decide intent, does not type/label entities,
    does not retrieve knowledge, and does not generate the
    customer-facing reply -- it only prepares a clean semantic signal
    for the engines that do. Still exactly one LLM call, one round
    trip -- no new call was added for the new fields.
    """

    async def understand(self, message: str, *, usage_out: dict[str, int | None] | None = None) -> LanguageResult:
        raw = await ai_service.complete_json(
            LANGUAGE_UNDERSTANDING_PROMPT.format(message=message),
            usage_out=usage_out,
        )

        detected = raw.get("detectedLanguage") or "other"
        reply_lang = raw.get("replyLanguage") or detected
        normalized = raw.get("normalizedMessage") or message
        confidence = float(raw.get("confidence", 0.5))

        communication_style = raw.get("communicationStyle") or "neutral"
        is_ambiguous = bool(raw.get("isAmbiguous", False))
        ambiguity_reason = raw.get("ambiguityReason") or None

        raw_spans = raw.get("entitySpans")
        entity_spans = (
            [str(s) for s in raw_spans if isinstance(s, str) and s.strip()]
            if isinstance(raw_spans, list)
            else []
        )

        return LanguageResult(
            original_message=message,
            detected_language=detected,
            reply_language=reply_lang,
            normalized_message=normalized,
            confidence=confidence,
            communication_style=communication_style,
            is_ambiguous=is_ambiguous,
            ambiguity_reason=ambiguity_reason,
            entity_spans=entity_spans,
        )


language_engine = LanguageEngine()
