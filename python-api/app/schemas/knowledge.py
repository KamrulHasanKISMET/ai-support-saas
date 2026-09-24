from pydantic import BaseModel


class IngestResponse(BaseModel):
    """Mirrors node-api's IngestResult interface (knowledge.client.ts) --
    camelCase on the wire, same as every other Node<->Python contract
    in this codebase (see AgentRequest/AgentResponse)."""

    status: str  # "READY" | "FAILED"
    chunkCount: int
    errorMessage: str | None = None
