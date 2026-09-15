"""
REQUEST METRICS MIDDLEWARE (Phase 1 observability).

Wraps every HTTP request python-api receives: assigns/reuses a
request id, times the request, records it in the in-memory counters
(app/core/metrics.py), and logs one structured line per request. This
is intentionally separate from anything route-specific (ai.py,
readiness.py, ...) -- no route handler needed to change to get this.

Mirrors node-api's middleware/requestId.ts + middleware/metrics.ts so
both services produce comparable request-level observability data.
"""

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.logging import logger
from app.core.metrics import record_request


class RequestMetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        incoming_id = request.headers.get("x-request-id")
        request_id = incoming_id if incoming_id else str(uuid.uuid4())
        request.state.request_id = request_id

        started_at = time.perf_counter()
        status_code = 500  # assume the worst until we know otherwise
        response = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_ms = (time.perf_counter() - started_at) * 1000
            record_request(status_code, duration_ms)
            logger.info(
                "http_request",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": status_code,
                    "latency_ms": round(duration_ms, 1),
                },
            )
            # Mutating headers here (before the deferred `return response`
            # above actually completes) still takes effect -- Starlette's
            # BaseHTTPMiddleware response object isn't serialized to the
            # wire until after this middleware returns, so this is the
            # standard place to attach a response header from `finally`.
            # Only possible on success paths -- if call_next() itself
            # raised, there's no response object to attach a header to,
            # and FastAPI's own exception handling takes over instead.
            if response is not None:
                response.headers["x-request-id"] = request_id
