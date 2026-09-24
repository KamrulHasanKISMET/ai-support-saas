"""
Tests for the Production Reliability workstream additions to python-api:
Prometheus metrics exposition and DB pool/query instrumentation.
Deliberately NOT re-testing Agent Run Trace / Kernel tracing -- that's
a different workstream (see docs/AGENT.md, test_observability.py).

NOTE on adaptation from the standalone Reliability zip: that zip's own
draft of this file tested a `GET /health/ready` endpoint that does not
exist in the integrated codebase -- ZIP #6's `GET /readiness` (see
app/api/routes/readiness.py, requires x-internal-secret) is the
authoritative, already-documented readiness contract (docs/API_CONTRACTS.md)
and was preserved instead of being replaced by a competing route. This
file tests the ACTUAL merged routing: GET /health (liveness, no auth),
GET /readiness (readiness, requires x-internal-secret), GET /metrics
(Prometheus, no auth -- see app/api/routes/metrics.py's docstring for
why).

Uses FastAPI's TestClient (sync wrapper over httpx, already a
dependency) against the real `app`, with the one real dependency
(Postgres, via the engine/AsyncSessionLocal) mocked out -- no live DB
needed to run these, matching the existing test_agent_foundation.py
convention of faking just the DB-shaped object rather than requiring a
real database.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app


class TestLiveness(unittest.TestCase):
    def test_health_returns_ok_without_touching_the_database(self):
        # No patching at all -- liveness must never depend on Postgres.
        client = TestClient(app)
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["service"], "python-api")
        # get_infra_snapshot() -- preserved Phase 1 behavior, not part
        # of the Reliability zip's simplified {status, service} shape.
        self.assertIn("process", body)


class TestReadiness(unittest.TestCase):
    """GET /readiness (ZIP #6's existing, documented contract) --
    requires x-internal-secret, checks Postgres via check_database_health()."""

    def test_ready_when_database_responds(self):
        headers = {"x-internal-secret": "test-secret"}
        with patch.object(settings, "internal_service_secret", "test-secret"), patch(
            "app.api.routes.readiness.check_database_health"
        ) as fake_check:
            fake_check.return_value = {"available": True, "pool_size": 5, "pool_checked_out": 0}
            client = TestClient(app)
            response = client.get("/readiness", headers=headers)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["dependencies"]["database"]["available"], True)

    def test_not_ready_when_database_raises(self):
        headers = {"x-internal-secret": "test-secret"}
        with patch.object(settings, "internal_service_secret", "test-secret"), patch(
            "app.api.routes.readiness.check_database_health"
        ) as fake_check:
            fake_check.return_value = {"available": False, "error": "connection refused"}
            client = TestClient(app)
            response = client.get("/readiness", headers=headers)

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["status"], "not_ready")
        self.assertEqual(body["dependencies"]["database"]["available"], False)

    def test_rejects_missing_secret(self):
        with patch.object(settings, "internal_service_secret", "test-secret"):
            client = TestClient(app)
            response = client.get("/readiness")
        self.assertEqual(response.status_code, 401)


class TestMetricsEndpoint(unittest.TestCase):
    def test_metrics_exposes_http_and_db_pool_series(self):
        fake_pool = MagicMock()
        fake_pool.size.return_value = 5
        fake_pool.checkedout.return_value = 1
        fake_pool.overflow.return_value = 0

        with patch("app.api.routes.metrics.engine") as fake_engine:
            fake_engine.pool = fake_pool
            client = TestClient(app)
            # Drive at least one request through the metrics middleware
            # before scraping, so http_requests_total has a data point.
            client.get("/health")
            response = client.get("/metrics")

        self.assertEqual(response.status_code, 200)
        text = response.text
        self.assertIn("http_requests_total", text)
        self.assertIn('route="/health"', text)
        self.assertIn("db_pool_size", text)
        self.assertIn("db_pool_checked_out_connections", text)
        self.assertIn("db_query_duration_seconds", text)

    def test_metrics_requires_no_auth(self):
        # Prometheus cannot present x-internal-secret -- confirms the
        # deliberate contract change documented in
        # app/api/routes/metrics.py and docs/API_CONTRACTS.md.
        with patch("app.api.routes.metrics.engine") as fake_engine:
            fake_engine.pool = MagicMock(size=lambda: 0, checkedout=lambda: 0, overflow=lambda: 0)
            client = TestClient(app)
            response = client.get("/metrics")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
