from app.ai.ai_service import ai_service
from app.language.language_types import LanguageResult

LANGUAGE_UNDERSTANDING_PROMPT = """You are a language-understanding preprocessor \
for a customer support AI. Customers write in ANY human language, in any script, in many different ways:

- Any language written natively (বাংলা, हिन्दी, العربية, español, 中文, 日本語,
  한국어, ไทย, Türkçe, Kiswahili, Русский, ... -- this list is only examples,
  never a limit)
- Romanized / transliterated text: a non-Latin language written in Latin
  letters (e.g. Bangla "amar order ta kobe ashbe?", Hindi "mera order kab
  aayega?", Arabic "wein talabi?", Thai "order khong chan yu nai?")
- Two or more languages mixed in one message (e.g. "আমার order ta কবে আসবে?",
  "mera order kab aayega, please tell me", "¿dónde está mi order? 谢谢")
- Informal spelling, abbreviations, typos, emoji, slang
- Short follow-up messages that only make sense in light of an earlier
  message you cannot see (e.g. "oita koto?" / "ওইটা কত?" / "और वो कितने का है?"
  -- "how much is that?")

CRITICAL RULE: Do NOT assume the customer's nationality, location, ethnicity, \
identity, or geography from the language or script they used. Some customers \
prefer English even in a Bangla-speaking market, and vice versa. Judge ONLY \
from the message itself. Language, script, and transliteration are NEVER a \
proxy for who someone is.

Your job has five parts:

1. Detect the language mix that was actually used, and decide what language \
   a reply should be written in (matching the customer's own communication \
   style, not a guessed locale).
2. Identify the writing system (script) actually used, the underlying language, \
   and whether the message is transliterated (e.g. Bangla written in Latin \
   letters = "Banglish" -- this is Bengali language in Latin script, not a \
   separate language). Also identify whether and how languages are mixed.
3. Produce a short, clear English paraphrase of what the customer means --
   preserving every fact exactly (product names, sizes, order IDs, numbers,
   dates). Do not translate proper nouns, codes, or SKUs. This paraphrase is
   used only internally for classification; it is never shown to the customer.
4. Judge the message's tone/register (communication style) and whether its
   meaning is ambiguous on its own -- most commonly because it refers to
   something ("that", "it", "ওইটা", "oইটা") with no antecedent inside this
   single message. A short follow-up like "oita koto?" with nothing else to
   go on IS ambiguous -- say so honestly rather than guessing what "that" is.
5. Mark (do NOT classify or label) any substrings of the ORIGINAL message
   that look like they could be entities -- product names, sizes, order IDs,
   quantities, dates, numbers. Copy them verbatim from the original message,
   in its original language/script. Do not decide what type each one is --
   that is a separate downstream step, not yours.

Respond ONLY with JSON, no preamble, no markdown fences:
{{
  "detectedLanguage": "bn" | "en" | "mixed" | "other",
  "language": "<open language tag>",
  "replyLanguage": "<open language tag>",
  "script": "<open script name>",
  "isTransliterated": true | false,
  "transliteratedFrom": "<open script name>" | null,
  "codeMixing": "none" | "inter_sentential" | "intra_sentential",
  "normalizedMessage": "...",
  "confidence": 0.0-1.0,
  "communicationStyle": "formal" | "informal" | "urgent" | "polite" | "neutral",
  "isAmbiguous": true | false,
  "ambiguityReason": "short reason, or null if not ambiguous",
  "entitySpans": ["substring1", "substring2", ...]
}}

Field guidance:
- language: the actual language(s) used, as an OPEN tag using the language's
  standard short code (bn, en, hi, ar, es, zh, ja, ko, th, tr, sw, ru, ur,
  ...) regardless of script. Any language is valid -- never force it into a
  fixed list. Use a comma-separated list (e.g. "hi,en") ONLY when the message
  genuinely mixes languages.
- replyLanguage: the same kind of open tag: the language the reply should be
  written in, matching how the customer wrote. Never "other" -- name the
  language.
- detectedLanguage: LEGACY field kept for backward compatibility only. Use
  "bn" or "en" if it applies, "mixed" for a mix, otherwise "other". All real
  language information belongs in `language`, not here.
- script: the writing system of the ORIGINAL message (NOT the normalizedMessage),
  as a lowercase name -- e.g. latin, bengali, devanagari, arabic, cyrillic,
  greek, hebrew, thai, hangul, hiragana, han, tamil, ... Any script is valid;
  do not force it into a fixed list. "mixed" when multiple scripts are present
  (e.g. "আমার order ta কবে আসবে?").
- isTransliterated: true when the message is written in Latin letters but the
  underlying language is normally written in a non-Latin script (e.g. "amar
  parcel ta koi?" = Bangla in Latin script; "mera order kab aayega" = Hindi in
  Latin script). false when the script matches the language natively.
- transliteratedFrom: only when isTransliterated=true. The script the customer
  would normally use for this language (open name, e.g. bengali, devanagari,
  arabic, cyrillic, thai). null otherwise.
- codeMixing: "none" = single language. "inter_sentential" = different languages
  in different sentences. "intra_sentential" = language switch within a sentence
  (the common Bangla+English case: "আমার order ta কোথায়?").

Customer message: {message}
"""


def _open_tag(value, max_len: int, *, lower: bool = False) -> str | None:
    """Defensive parse for OPEN-set string fields (language, script, ...).

    Deliberately does NOT check against any list of known values -- that is
    exactly the closed-enum ceiling docs/GENERAL_LANGUAGE_BRAIN.md §5.1
    removes. It only guarantees: a non-empty string, trimmed, optionally
    lowercased, and truncated to the DB column width (a too-long value
    would otherwise make the INSERT fail, and the isolated write path
    would silently drop the whole learning row). Returns None if unusable.
    """
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    if lower:
        v = v.lower()
    return v[:max_len]


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

    async def understand(
        self, message: str, *, usage_out: dict[str, int | None] | None = None
    ) -> LanguageResult:
        raw = await ai_service.complete_json(
            LANGUAGE_UNDERSTANDING_PROMPT.format(message=message),
            usage_out=usage_out,
        )

        detected = _open_tag(raw.get("detectedLanguage"), 20) or "other"
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

        # ── Language-agnostic schema fields (§5.2) ──────────────────
        # Defensive parsing: each field falls back to a safe default if
        # absent or malformed -- same discipline as every other field
        # above and in docs/LANGUAGE.md. The LLM may omit these on very
        # short messages or unexpected input; callers must not assume
        # presence.
        _VALID_CODE_MIXING = {"none", "inter_sentential", "intra_sentential"}

        # `script` is an OPEN set (docs/GENERAL_LANGUAGE_BRAIN.md §5.2):
        # Thai, Hebrew, Hangul, Cyrillic, ... must never be forced into
        # "latin" just because they are not on a hard-coded list. Only
        # type/empty/length are checked. "und" = not reported, an honest
        # "don't know" rather than a false claim of Latin script.
        script = _open_tag(raw.get("script"), 20, lower=True) or "und"

        is_transliterated = bool(raw.get("isTransliterated", False))

        transliterated_from = (
            _open_tag(raw.get("transliteratedFrom"), 20, lower=True)
            if is_transliterated
            else None
        )

        raw_mixing = raw.get("codeMixing")
        code_mixing = (
            raw_mixing
            if isinstance(raw_mixing, str) and raw_mixing in _VALID_CODE_MIXING
            else "none"
        )

        # ── Open language tag (§5.2 row 1, added after script/
        # transliteration/code_mixing shipped) ──────────────────────
        # Deliberately NOT validated against a fixed set -- §5.1's whole
        # point is that "language" must never be a closed enum the way
        # detectedLanguage is. Only defensive against type/empty-string
        # garbage, same discipline as every other field here. A comma-
        # separated multi-tag string (e.g. "bn,en") is accepted as-is
        # when the LLM reports genuine mixing; this function does not
        # need to parse it apart, only pass through what the LLM said.
        language = _open_tag(raw.get("language"), 30) or "und"

        # replyLanguage: open tag. If the LLM omitted it, fall back to the
        # primary tag of `language` (so a Hindi customer is answered in
        # Hindi), and only then to the legacy detectedLanguage bucket.
        primary_language = language.split(",")[0].strip()
        reply_lang = (
            _open_tag(raw.get("replyLanguage"), 20)
            or (primary_language if primary_language != "und" else None)
            or detected
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
            # Language-agnostic schema (§5.2):
            script=script,
            is_transliterated=is_transliterated,
            transliterated_from=transliterated_from,
            code_mixing=code_mixing,
            language=language,
        )


language_engine = LanguageEngine()
