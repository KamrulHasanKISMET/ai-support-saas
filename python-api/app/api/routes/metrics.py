"""
GET /metrics -- Prometheus text exposition format (Production
Reliability workstream), scraped by monitoring/prometheus.yml.

Previously required x-internal-secret and returned a plain JSON
snapshot (app/core/metrics.py's get_request_metrics_snapshot /
get_infra_snapshot). Prometheus has no way to present that secret on
a scrape, so this endpoint is now UNAUTHENTICATED, matching node-api's
equivalent (node-api/src/modules/health/health.routes.ts) -- see
docs/RELIABILITY.md "Metrics security" for why that's an acceptable
trade (internal docker-compose network only, never published
publicly) and what a production deployment should add in front of it
(reverse-proxy allowlist / internal-only network segment).

The underlying JSON counters aren't gone -- get_infra_snapshot() still
backs the `process` field on GET /health and GET /readiness.
"""

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.database import engine
from app.core.metrics import db_pool_checked_out, db_pool_overflow, db_pool_size, registry

router = APIRouter()


@router.get("/metrics")
async def metrics():
    pool = engine.pool
    try:
        db_pool_size.set(pool.size())
        db_pool_checked_out.set(pool.checkedout())
        db_pool_overflow.set(pool.overflow())
    except Exception:
        # Pool introspection must never break a scrape -- an empty/odd
        # reading here still leaves the HTTP-level series usable.
        pass

    return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
