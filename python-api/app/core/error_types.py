"""
ERROR CLASSIFICATION — shared taxonomy for structured logs and the
Agent Run Trace's `error_category` column.

This does NOT change how errors are caught or handled anywhere --
every existing try/except in kernel.py, context_engine.py, etc. keeps
its exact same control flow. This module only gives those existing
catch blocks a consistent, greppable LABEL to attach when they log,
instead of free-text error messages that vary by call site.

Mirrored in node-api as src/utils/errorCategory.ts (TypeScript can't
share a Python enum directly, so the two are kept in sync by hand --
the string VALUES are identical across both, which is what actually
matters for log aggregation/search).
"""

from enum import StrEnum


class ErrorCategory(StrEnum):
    API_ERROR = "api_error"
    DATABASE_ERROR = "database_error"
    REDIS_ERROR = "redis_error"  # not currently emitted by python-api (no Redis dependency here yet -- see docs/OBSERVABILITY.md), kept for taxonomy parity with node-api
    LLM_ERROR = "llm_error"
    RAG_ERROR = "rag_error"
    MEMORY_ERROR = "memory_error"
    TIMEOUT = "timeout"
    VALIDATION_ERROR = "validation_error"


def classify_exception(exc: BaseException) -> ErrorCategory:
    """
    Best-effort label for an already-caught exception, based on where
    it came from (its type's module) -- used ONLY for the trace's
    `error_category` column and structured logs. Does NOT change how
    any existing try/except handles the exception; this is called
    alongside the existing handling, never instead of it.

    Order matters: TimeoutError is checked before the module-based
    checks since httpx/asyncpg/anthropic all raise their own
    timeout subclasses that also match their library's module check --
    checking the more specific "was this a timeout" question first
    gives a more useful category than the generic per-library one.
    """
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return ErrorCategory.TIMEOUT

    module = type(exc).__module__.lower()
    if "anthropic" in module or "openai" in module:
        return ErrorCategory.LLM_ERROR
    if "sqlalchemy" in module or "asyncpg" in module or "psycopg" in module:
        return ErrorCategory.DATABASE_ERROR
    if "redis" in module:
        return ErrorCategory.REDIS_ERROR
    if isinstance(exc, (ValueError, TypeError)):
        return ErrorCategory.VALIDATION_ERROR

    return ErrorCategory.API_ERROR
