from pydantic import BaseModel

# These mirror app/schemas/kernel.py's KernelRunRequest/Response field-for-field
# (tenantId, customerId, conversationId, messageId, message, reply, intent,
# confidence, state, toolsCalled) and only ADD optional fields (channel,
# metadata) with defaults. Node.js's existing POST /ai/kernel/run payload
# and expected response shape both remain valid against these models —
# no changes required in node-api/src/modules/ai/ai.client.ts.


class AgentRequest(BaseModel):
    """Stable input contract for the Core Agent (integration doc section 9).
    tenantId/customerId/conversationId/messageId must always travel together
    — they are foundational for multi-tenancy, memory, state, and future
    observability/auditability."""

    tenantId: int
    customerId: int
    conversationId: int
    messageId: int
    message: str

    channel: str | None = None
    metadata: dict = {}
    # Propagated from node-api's x-request-id header (see
    # middleware/requestId.ts) so one customer interaction can be traced
    # across both services in the logs (architecture doc section 33).
    requestId: str | None = None


class AgentResponse(BaseModel):
    """Stable output contract for the Core Agent (integration doc section 10).
    Structured rather than a raw string, so callers can branch on intent/
    confidence/toolsCalled without re-parsing text."""

    reply: str
    intent: str | None = None
    confidence: float | None = None
    state: dict = {}
    toolsCalled: list[str] = []
    metadata: dict = {}
