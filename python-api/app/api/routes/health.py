from fastapi import APIRouter

from app.core.metrics import get_infra_snapshot

router = APIRouter()


@router.get("/health")
async def health():
    """
    "Is the process alive?" -- deliberately makes NO dependency calls
    (no DB, no Redis, no LLM). See GET /readiness for "can this service
    currently serve requests?", which does check dependencies.
    """
    return {"status": "ok", "service": "python-api", "process": get_infra_snapshot()}
