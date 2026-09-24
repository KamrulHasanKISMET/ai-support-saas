"""
Tests for app/core/trace_sanitize.py -- the guard that stands between
a raw exception and anything written into agent_run_traces.steps.

These are the most important tests added in this phase: a bug here
means a credential could land in the database, potentially readable
by anyone with SQL access to agent_run_traces. Every pattern this
module claims to redact gets an explicit test.
"""

import unittest

from app.core.trace_sanitize import safe_error_message


class TestSafeErrorMessage(unittest.TestCase):
    def test_redacts_postgres_dsn_credentials(self):
        exc = Exception(
            "could not connect to server: Connection refused "
            "(postgresql://myuser:MySecretPass123@localhost:5432/db)"
        )
        result = safe_error_message(exc)
        self.assertNotIn("MySecretPass123", result)
        self.assertNotIn("myuser:MySecretPass123", result)
        self.assertIn("[REDACTED]", result)

    def test_redacts_redis_dsn_credentials(self):
        exc = Exception("Error connecting to redis://default:hunter2@redis-host:6379/0")
        result = safe_error_message(exc)
        self.assertNotIn("hunter2", result)
        self.assertIn("[REDACTED]", result)

    def test_redacts_bearer_token(self):
        exc = Exception("Request failed: Authorization: Bearer sk-ant-abcdef1234567890 was rejected")
        result = safe_error_message(exc)
        self.assertNotIn("sk-ant-abcdef1234567890", result)

    def test_redacts_bearer_without_authorization_prefix(self):
        exc = Exception("got 401, sent header 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'")
        result = safe_error_message(exc)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", result)

    def test_redacts_api_key_field(self):
        exc = Exception("OpenAI request failed, api_key=sk-proj-verySecretValue123 invalid")
        result = safe_error_message(exc)
        self.assertNotIn("verySecretValue123", result)

    def test_redacts_password_field(self):
        exc = Exception("auth failed: password=hunter2plus")
        result = safe_error_message(exc)
        self.assertNotIn("hunter2plus", result)

    def test_redacts_generic_secret_key_style_token(self):
        exc = Exception("invalid key sk-abcdefghij1234567890")
        result = safe_error_message(exc)
        self.assertNotIn("sk-abcdefghij1234567890", result)

    def test_ordinary_message_passes_through_readable(self):
        exc = ValueError("tenantId must be a positive integer")
        result = safe_error_message(exc)
        self.assertIn("tenantId must be a positive integer", result)
        self.assertIn("ValueError", result)

    def test_includes_exception_class_name(self):
        exc = ConnectionError("timed out")
        result = safe_error_message(exc)
        self.assertTrue(result.startswith("ConnectionError:"))

    def test_truncates_long_messages(self):
        exc = Exception("x" * 1000)
        result = safe_error_message(exc)
        self.assertLessEqual(len(result), 350)  # 300 + class name + "...[truncated]"
        self.assertIn("truncated", result)

    def test_never_raises_on_unprintable_exception(self):
        class Unprintable(Exception):
            def __str__(self):
                raise RuntimeError("cannot stringify")

        try:
            result = safe_error_message(Unprintable())
        except Exception as exc:  # pragma: no cover
            self.fail(f"safe_error_message() raised unexpectedly: {exc}")
        self.assertIn("unprintable", result.lower())

    def test_multiple_secrets_in_one_message_all_redacted(self):
        exc = Exception(
            "connect postgresql://u:p1@host/db failed, retry with "
            "Authorization: Bearer tok123, api_key=sk-live-999"
        )
        result = safe_error_message(exc)
        for leaked in ["p1@host", "tok123", "sk-live-999"]:
            self.assertNotIn(leaked, result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
