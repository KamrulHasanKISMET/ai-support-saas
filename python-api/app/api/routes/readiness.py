"""
GET /readiness -- "can this service currently serve requests?"

Distinct from GET /health ("is the process alive?" -- health.py).
A process can be alive (health passes) while genuinely unable to serve
a request (e.g. Postgres unreachable) -- readiness is what should gate
traffic/orchestration decisions, health is what should gate
"should this container be restarted".

python-api's only real external dependency is Postgres (see
AI_CONTEXT.md/PROJECT_STATUS.md -- `redis` is in requirements.txt but
never imported/used anywhere in this service). This endpoint does NOT
fabricate a Redis check for a dependency this service doesn't have;
Redis readiness belongs to node-api's /readiness instead, where it's
actually used (rate limiting, caching).

No auth required (same as /health) -- container orchestration tooling
calling this won't have x-internal-secret either.

UPDATE: this repo's own documented rule (docs/API_CONTRACTS.md) is
"every python-api route except /health requires x-internal-secret".
To stay consistent with that already-established contract rather than
carve out a silent new exception, /readiness DOES require the secret
here. If real infrastructure (a load balancer, k8s probe) needs to
call this unauthenticated later, that's a deliberate, separately
reviewed change -- not an implicit side effect of adding this endpoint.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.core.database import check_database_health
from app.core.metrics import get_infra_snapshot
from app.core.security import require_internal_secret

router = APIRouter(dependencies=[Depends(require_internal_secret)])


@router.get("/readiness")
async def readiness():
    db_health = await check_database_health()
    ready = db_health["available"]

    body = {
        "status": "ready" if ready else "not_ready",
        "service": "python-api",
        "dependencies": {
            "database": db_health,
        },
        "process": get_infra_snapshot(),
    }
    if ready:
        return body
    return JSONResponse(content=body, status_code=503)
