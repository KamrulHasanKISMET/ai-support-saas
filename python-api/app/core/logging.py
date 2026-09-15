import json
import logging
import sys


class JsonFormatter(logging.Formatter):
    """
    Wraps whatever a log call already produces into a JSON line with
    timestamp/service/level -- WITHOUT requiring any existing
    `logger.info("...", arg1, arg2)` call site anywhere in the codebase
    (kernel.py, ai_service.py, etc.) to change. `record.getMessage()`
    applies the existing %-style formatting exactly as logging.basicConfig
    already did; this formatter only changes the OUTER envelope.

    Structured fields (request_id, tenant_id, agent_run_id, latency_ms,
    error_category, ...) can additionally be attached via the stdlib
    `extra={...}` kwarg on new call sites (see request_middleware.py,
    kernel.py's new timing lines) -- those end up as extra attributes
    on the LogRecord, which this formatter picks up and includes.

    Never logs API keys, passwords, JWT secrets, or internal secrets --
    that discipline is enforced by NOT passing those values into any
    logger.*() call in the first place (see docs/OBSERVABILITY.md for
    the explicit checklist), not by redaction here.
    """

    # Attributes every LogRecord has by default -- anything else set on
    # a record (via `extra=`) is a caller-supplied structured field and
    # should be included in the JSON output.
    _STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys())

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "service": "python-api",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key in self._STANDARD_ATTRS or key in payload:
                continue
            payload[key] = value

        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """
    Structured JSON logging (Phase 1 observability). Every existing
    `logger.info(...)`/`logger.error(...)` call across the codebase
    keeps working unchanged -- only the output envelope changed, from
    a plain-text line to a JSON line with timestamp/service/level.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


logger = logging.getLogger("ai_service")
