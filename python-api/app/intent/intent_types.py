from enum import StrEnum


class IntentType(StrEnum):
    """Initial intent set (architecture doc section 17). Extend per vertical."""

    ORDER_STATUS = "ORDER_STATUS"
    PRODUCT_INFO = "PRODUCT_INFO"
    PRODUCT_AVAILABILITY = "PRODUCT_AVAILABILITY"
    PRICE_INQUIRY = "PRICE_INQUIRY"
    DELIVERY_INFO = "DELIVERY_INFO"
    RETURN_REQUEST = "RETURN_REQUEST"
    COMPLAINT = "COMPLAINT"
    NEGOTIATION = "NEGOTIATION"
    CREATE_ORDER = "CREATE_ORDER"
    GENERAL_QUESTION = "GENERAL_QUESTION"


class IntentResult:
    """
    Output of the Intent Engine. Deliberately NOT a final business
    decision (section 17) — the Kernel decides what to do with it.
    """

    def __init__(self, intent: IntentType, confidence: float, entities: dict):
        self.intent = intent
        self.confidence = confidence
        self.entities = entities

    def to_dict(self) -> dict:
        return {
            "intent": self.intent.value,
            "confidence": self.confidence,
            "entities": self.entities,
        }
