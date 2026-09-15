"""
GET /metrics -- plain JSON snapshot of the in-memory counters from
app/core/metrics.py. NOT Prometheus/OpenMetrics format (explicitly out
of scope for this phase) -- just enough to see request volume/error
rate/latency without standing up a monitoring stack yet.

Requires x-internal-secret, same as every other python-api route
except /health (see readiness.py's docstring for why this endpoint
follows that rule too).
"""

from fastapi import APIRouter, Depends

from app.core.metrics import get_infra_snapshot, get_request_metrics_snapshot
from app.core.security import require_internal_secret

router = APIRouter(dependencies=[Depends(require_internal_secret)])


@router.get("/metrics")
async def metrics():
    return {
        "requests": get_request_metrics_snapshot(),
        "process": get_infra_snapshot(),
    }
