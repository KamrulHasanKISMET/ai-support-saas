from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.core_agent import core_agent
from app.core.database import get_db
from app.core.security import require_internal_secret
from app.schemas.agent import AgentRequest, AgentResponse

router = APIRouter(
    prefix="/ai/kernel", tags=["kernel"], dependencies=[Depends(require_internal_secret)]
)


@router.post("/run", response_model=AgentResponse)
async def run_kernel(
    payload: AgentRequest,
    db: AsyncSession = Depends(get_db),
    x_request_id: str | None = Header(default=None),
) -> AgentResponse:
    """
    Single entrypoint the Node.js `ai.client.ts` calls for every inbound
    customer message, regardless of channel.

    URL and JSON shape are UNCHANGED from before — node-api needs no
    changes. Internally, this now routes through the Core Agent
    (app/agent/core_agent.py), which delegates to the same Kernel
    (app/kernel/kernel.py) as before. AgentRequest/AgentResponse are a
    strict superset of the old KernelRunRequest/KernelRunResponse
    (same required fields, only new fields are optional with defaults),
    so this is a non-breaking change.
    """
    if payload.requestId is None:
        payload.requestId = x_request_id
    return await core_agent.run(db=db, request=payload)
