from enum import StrEnum


class ConversationStatus(StrEnum):
    """Coarse conversation status (architecture doc section 20)."""

    NEW = "NEW"
    PRODUCT_DISCUSSION = "PRODUCT_DISCUSSION"
    PRODUCT_SELECTED = "PRODUCT_SELECTED"
    SIZE_SELECTED = "SIZE_SELECTED"
    ORDER_READY = "ORDER_READY"
    ORDER_CREATED = "ORDER_CREATED"
    RETURN_DISCUSSION = "RETURN_DISCUSSION"
    NEGOTIATION = "NEGOTIATION"
    ESCALATED = "ESCALATED"


# Fine-grained state slots accumulated across turns, e.g.
# {"product": "Nike Air Max", "size": "42", "quantity": 1}
StateSlots = dict[str, str]
