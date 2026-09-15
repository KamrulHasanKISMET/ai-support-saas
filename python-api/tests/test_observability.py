"""
Tests for Phase 1 observability's pure-logic modules: error
classification (app/core/error_types.py) and the metrics counters
(app/core/metrics.py).

Deliberately mirrors node-api's src/utils/errorCategory.test.ts and
src/utils/metrics.test.ts in scope and structure -- both services'
observability foundations should have comparable test coverage. Like
those, this does NOT test the middleware wiring itself
(request_middleware.py needs a real Starlette app/ASGI client, which
is integration-test territory) -- it tests the classification/counting
LOGIC those middlewares call into, the same boundary node-api's tests
draw around middleware/metrics.ts vs utils/metrics.ts.

No app dependencies beyond stdlib are needed for these two modules
(confirmed by reading their imports), so unlike test_language_engine.py
/test_agent_foundation.py this file needs no anthropic/pydantic_settings
stubs -- it only needs `app` importable, which still requires the
sqlalchemy stub (transitively, via app.core.config -> ... -> no,
actually error_types.py and metrics.py have zero app-internal imports
either). Run the same way as the other test files for consistency:
    PYTHONPATH=<stubs>:. python -m unittest tests.test_observability -v
"""

import unittest

from app.core.error_types import ErrorCategory, classify_exception
from app.core.metrics import get_infra_snapshot, get_request_metrics_snapshot, record_request


def make_exc(module_name: str, name: str = "FakeError", bases=(Exception,)):
    """Builds an exception instance whose class reports a chosen
    `__module__` -- lets us simulate "an exception raised by the
    anthropic/sqlalchemy/redis library" without those packages
    actually being installed, since classify_exception() only ever
    inspects type(exc).__module__ and the class name."""
    cls = type(name, bases, {"__module__": module_name})
    return cls("boom")


class TestClassifyException(unittest.TestCase):
    def test_builtin_timeout_error(self):
        self.assertEqual(classify_exception(TimeoutError("timed out")), ErrorCategory.TIMEOUT)

    def test_custom_timeout_named_exception(self):
        # e.g. httpx.ReadTimeout, asyncio.TimeoutError subclasses --
        # matched by name even when not a TimeoutError subclass.
        exc = make_exc("httpx", name="ReadTimeout")
        self.assertEqual(classify_exception(exc), ErrorCategory.TIMEOUT)

    def test_anthropic_module_is_llm_error(self):
        exc = make_exc("anthropic._exceptions", name="APIStatusError")
        self.assertEqual(classify_exception(exc), ErrorCategory.LLM_ERROR)

    def test_openai_module_is_llm_error(self):
        exc = make_exc("openai.error", name="RateLimitError")
        self.assertEqual(classify_exception(exc), ErrorCategory.LLM_ERROR)

    def test_sqlalchemy_module_is_database_error(self):
        exc = make_exc("sqlalchemy.exc", name="IntegrityError")
        self.assertEqual(classify_exception(exc), ErrorCategory.DATABASE_ERROR)

    def test_asyncpg_module_is_database_error(self):
        exc = make_exc("asyncpg.exceptions", name="UniqueViolationError")
        self.assertEqual(classify_exception(exc), ErrorCategory.DATABASE_ERROR)

    def test_redis_module_is_redis_error(self):
        exc = make_exc("redis.exceptions", name="ConnectionError")
        self.assertEqual(classify_exception(exc), ErrorCategory.REDIS_ERROR)

    def test_value_error_is_validation_error(self):
        self.assertEqual(classify_exception(ValueError("bad input")), ErrorCategory.VALIDATION_ERROR)

    def test_type_error_is_validation_error(self):
        self.assertEqual(classify_exception(TypeError("wrong type")), ErrorCategory.VALIDATION_ERROR)

    def test_unrecognized_exception_falls_back_to_api_error(self):
        exc = make_exc("some.random.module", name="WeirdError")
        self.assertEqual(classify_exception(exc), ErrorCategory.API_ERROR)

    def test_timeout_check_has_priority_over_module_based_checks(self):
        # A timeout raised BY an anthropic/sqlalchemy call should still
        # classify as TIMEOUT, not LLM_ERROR/DATABASE_ERROR -- "what
        # actually went wrong" beats "which library raised it".
        exc = make_exc("anthropic._exceptions", name="APITimeoutError")
        self.assertEqual(classify_exception(exc), ErrorCategory.TIMEOUT)

        exc2 = make_exc("sqlalchemy.exc", name="TimeoutError")
        self.assertEqual(classify_exception(exc2), ErrorCategory.TIMEOUT)

    def test_never_raises_on_an_unusual_exception_shape(self):
        # BaseException subclass that isn't a plain Exception (e.g.
        # KeyboardInterrupt-like) must still classify without raising.
        class Weird(BaseException):
            pass

        try:
            result = classify_exception(Weird("boom"))
        except Exception as exc:  # pragma: no cover
            self.fail(f"classify_exception() raised unexpectedly: {exc}")
        self.assertEqual(result, ErrorCategory.API_ERROR)


class TestMetrics(unittest.TestCase):
    """
    Like metrics.py's own module docstring notes, these counters are a
    shared, module-level singleton (matching the real app's behavior).
    Tests record a known delta and assert on that delta, rather than
    assuming a pristine zero starting point -- keeps tests
    order-independent even though state is shared, same approach as
    node-api's metrics.test.ts.
    """

    def test_record_request_increments_total_count(self):
        before = get_request_metrics_snapshot()["total_count"]
        record_request(200, 10.0)
        after = get_request_metrics_snapshot()["total_count"]
        self.assertEqual(after, before + 1)

    def test_record_request_counts_5xx_as_errors_only(self):
        before = get_request_metrics_snapshot()
        record_request(200, 5.0)
        record_request(404, 5.0)
        record_request(500, 5.0)
        record_request(503, 5.0)
        after = get_request_metrics_snapshot()
        self.assertEqual(after["error_count"], before["error_count"] + 2)
        self.assertEqual(after["total_count"], before["total_count"] + 4)

    def test_record_request_tracks_per_status_code(self):
        before = get_request_metrics_snapshot()["status_counts"].get(201, 0)
        record_request(201, 3.0)
        record_request(201, 7.0)
        after = get_request_metrics_snapshot()["status_counts"].get(201, 0)
        self.assertEqual(after, before + 2)

    def test_avg_duration_ms_is_non_negative(self):
        record_request(299, 100.0)
        record_request(299, 200.0)
        snapshot = get_request_metrics_snapshot()
        self.assertGreaterEqual(snapshot["avg_duration_ms"], 0)

    def test_get_infra_snapshot_returns_sane_values(self):
        snapshot = get_infra_snapshot()
        self.assertGreaterEqual(snapshot["uptime_seconds"], 0)
        self.assertGreaterEqual(snapshot["cpu_time_seconds"], 0)
        # memory_rss_mb can in principle be None if even the ru_maxrss
        # fallback fails, but on any real Linux container (which is
        # the only place this ever runs -- see docker-compose.yml) it
        # should be a positive number.
        self.assertIsNotNone(snapshot["memory_rss_mb"])
        self.assertGreater(snapshot["memory_rss_mb"], 0)

    def test_get_infra_snapshot_never_raises(self):
        try:
            get_infra_snapshot()
        except Exception as exc:  # pragma: no cover
            self.fail(f"get_infra_snapshot() raised unexpectedly: {exc}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
