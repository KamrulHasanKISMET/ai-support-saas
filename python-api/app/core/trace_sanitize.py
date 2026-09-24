"""
TRACE SANITIZATION.

Responsibility: turn an exception into a short, trace-safe string --
class name + a redacted, truncated message. Used anywhere a NEW
per-step error field is written into agent_run_traces (the existing
top-level `error` column is untouched -- it was already just a static
label like "kernel_fallback", never raw exception text, so it needed
no sanitization and still doesn't).

Redaction happens BEFORE truncation, not after -- truncating first
could leave a partial secret sitting right at the cut point.

This is pattern-based best-effort redaction, not a full DLP system --
deliberately minimal ("smallest necessary"), matching
docs/OBSERVABILITY.md's existing scope decisions. If a new kind of
credential starts appearing in error messages, add a pattern here
rather than building a general-purpose secret scanner.
"""

import re

_MAX_ERROR_LENGTH = 300

_CREDENTIAL_PATTERNS = [
    # postgresql://user:password@host -- DSN-style credentials that can
    # appear verbatim in a driver's connection-error message.
    re.compile(r"://[^\s/]+:[^\s/@]+@", re.IGNORECASE),
    # "Bearer <token>" -- MUST run before the generic Authorization
    # pattern below. A combined authorization|bearer alternation would
    # let "Authorization:" greedily match just the word "Bearer" as its
    # own \S+ capture, leaving the actual token right after it exposed.
    re.compile(r"(?i)\bbearer\s+\S+"),
    # Any remaining "Authorization: <value>" not already redacted above
    # (e.g. a raw header value with no "Bearer" scheme prefix).
    re.compile(r"(?i)\bauthorization\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd)\b\s*[:=]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),  # Anthropic/OpenAI-style secret key prefix
]


def safe_error_message(exc: BaseException) -> str:
    """`ExceptionClassName: redacted, truncated message`. Never raises
    -- worst case (an unprintable exception) returns a generic string
    rather than propagating a second failure while trying to record
    the first one."""
    try:
        message = str(exc)
    except Exception:
        message = "<unprintable exception>"

    for pattern in _CREDENTIAL_PATTERNS:
        message = pattern.sub("[REDACTED]", message)

    text = f"{type(exc).__name__}: {message}"
    if len(text) > _MAX_ERROR_LENGTH:
        text = text[:_MAX_ERROR_LENGTH] + "...[truncated]"
    return text
