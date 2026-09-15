from fastapi import Header, HTTPException

from app.core.config import settings


async def require_internal_secret(x_internal_secret: str | None = Header(default=None)) -> None:
    """
    FastAPI dependency: verifies the caller presented the shared secret
    node-api is configured with (INTERNAL_SERVICE_SECRET in both
    services' .env). Without this, anything that can reach this
    service's port could call /ai/kernel/run directly — bypassing
    node-api's x-api-key check and rate limiting entirely, including
    triggering billed LLM calls (see docs/API_CONTRACTS.md).

    If INTERNAL_SERVICE_SECRET is left empty (e.g. a from-scratch local
    setup before configuring it), this fails CLOSED — every request is
    rejected — rather than silently allowing everything through.
    """
    if not settings.internal_service_secret:
        raise HTTPException(
            status_code=503,
            detail="INTERNAL_SERVICE_SECRET is not configured on this service.",
        )
    if x_internal_secret != settings.internal_service_secret:
        raise HTTPException(status_code=401, detail="Invalid or missing x-internal-secret.")
