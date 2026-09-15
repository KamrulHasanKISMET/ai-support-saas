from app.ai.ai_service import ai_service
from app.intent.intent_types import IntentResult, IntentType

INTENT_CLASSIFICATION_PROMPT = """You are an intent classifier for a customer \
support AI. Classify the customer's message into exactly one of these intents:
{intents}
{entity_hint_block}
Respond ONLY with JSON: {{"intent": "...", "confidence": 0.0-1.0, "entities": {{}}}}
No preamble, no markdown fences.

Customer message: {message}
"""

ENTITY_HINT_TEMPLATE = """
A preprocessing step already noticed these possible entity-like spans in \
the message (unverified, untyped -- confirm, retype, ignore, or extend them \
as you see fit; do not treat this list as authoritative): {spans}
"""


class IntentEngine:
    """
    Classifies "what does the customer want right now?" (section 17).
    Does NOT make the final business decision — that belongs to the
    Kernel's Reasoning/Decision step.

    Owns all entity TYPING/LABELING (the "entities" dict in its
    result) — this stays true even with entity_hints below, which are
    only untyped candidate substrings from the Language Engine
    (LanguageResult.entity_spans). Language Engine marks boundaries;
    Intent Engine decides what each one means. Passing hints is
    optional and additive — omitting entity_hints behaves exactly as
    before this parameter existed.
    """

    async def classify(
        self, message: str, entity_hints: list[str] | None = None
    ) -> IntentResult:
        intents = ", ".join(i.value for i in IntentType)
        entity_hint_block = (
            ENTITY_HINT_TEMPLATE.format(spans=entity_hints) if entity_hints else ""
        )
        prompt = INTENT_CLASSIFICATION_PROMPT.format(
            intents=intents, entity_hint_block=entity_hint_block, message=message
        )

        raw = await ai_service.complete_json(prompt)

        try:
            intent = IntentType(raw.get("intent", IntentType.GENERAL_QUESTION.value))
        except ValueError:
            intent = IntentType.GENERAL_QUESTION

        confidence = float(raw.get("confidence", 0.0))
        entities = raw.get("entities", {}) or {}

        return IntentResult(intent=intent, confidence=confidence, entities=entities)


intent_engine = IntentEngine()
