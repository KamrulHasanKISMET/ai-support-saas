from fastapi import FastAPI

from app.api.routes import ai, health, memory, metrics, rag, readiness
from app.core.logging import configure_logging
from app.core.request_middleware import RequestMetricsMiddleware

configure_logging()

app = FastAPI(
    title="AI Support SaaS — AI Service",
    description="Kernel, RAG, Memory, Intent, and State engines for the "
    "Multi-Tenant AI Customer Support SaaS.",
    version="0.1.0",
)

# Phase 1 observability: assigns/reuses request_id, times every request,
# records it in app/core/metrics.py's in-memory counters, logs one
# structured line per request. Added first so it wraps every route below.
app.add_middleware(RequestMetricsMiddleware)

app.include_router(health.router)
app.include_router(readiness.router)
app.include_router(metrics.router)
app.include_router(ai.router)
app.include_router(rag.router)
app.include_router(memory.router)
