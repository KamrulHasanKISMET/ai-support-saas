import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.metrics import http_errors_total, http_request_duration_seconds, http_requests_total


class MetricsMiddleware(BaseHTTPMiddleware):
    """
    Records method/route/status_code/duration for every request.

    Route label uses the matched route path template (e.g.
    `/ai/kernel/run`), read from `request.scope["route"]` AFTER
    `call_next` has run the request through routing — this keeps label
    cardinality bounded to the app's real route patterns rather than
    raw paths, mirroring node-api's `middleware/metrics.ts`. Requests
    that don't match any route (404s) fall back to the raw path, an
    accepted, bounded source of extra label values for a service with
    a small number of real 404 sources.
    """

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response: Response = await call_next(request)
        duration = time.perf_counter() - start

        route = request.scope.get("route")
        route_path = route.path if route is not None else request.url.path
        labels = {
            "method": request.method,
            "route": route_path,
            "status_code": str(response.status_code),
        }

        http_request_duration_seconds.labels(**labels).observe(duration)
        http_requests_total.labels(**labels).inc()
        if response.status_code >= 400:
            http_errors_total.labels(**labels).inc()

        return response
