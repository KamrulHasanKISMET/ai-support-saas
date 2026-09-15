/**
 * ERROR CLASSIFICATION — shared taxonomy for structured logs.
 *
 * Mirrors python-api/app/core/error_types.py's ErrorCategory enum --
 * the string VALUES are identical across both services on purpose
 * (that's what makes log aggregation/search across node-api and
 * python-api actually useful together). TypeScript can't share a
 * Python enum directly, so the two are kept in sync by hand.
 *
 * This does NOT change how errors are caught or handled anywhere --
 * every existing try/catch (errorHandler.ts, ai.client.ts, etc.) keeps
 * its exact same control flow. This module only gives those existing
 * catch blocks a consistent, greppable LABEL to attach when they log.
 */

export enum ErrorCategory {
  API_ERROR = "api_error",
  DATABASE_ERROR = "database_error",
  REDIS_ERROR = "redis_error",
  LLM_ERROR = "llm_error",
  RAG_ERROR = "rag_error",
  MEMORY_ERROR = "memory_error",
  TIMEOUT = "timeout",
  VALIDATION_ERROR = "validation_error",
}

/**
 * Best-effort label for an already-caught error, based on its
 * constructor name / message shape. Used ONLY for structured logs --
 * does NOT change how any existing catch block handles the error.
 *
 * node-api never talks to an LLM/RAG/Memory engine directly (that's
 * python-api's job — see AI_CONTEXT.md), so LLM_ERROR/RAG_ERROR/
 * MEMORY_ERROR are near-unreachable here in practice; kept for
 * taxonomy parity with python-api's version and in case a future
 * node-api call site needs them (e.g. classifying a failure surfaced
 * back from ai.client.ts's Kernel call).
 */
export function classifyError(err: unknown): ErrorCategory {
  if (!(err instanceof Error)) {
    return ErrorCategory.API_ERROR;
  }

  const name = err.name.toLowerCase();
  const message = err.message.toLowerCase();

  if (name.includes("timeout") || message.includes("timeout") || message.includes("etimedout")) {
    return ErrorCategory.TIMEOUT;
  }

  // pg (node-postgres) error codes/names, and generic driver hints.
  if (
    name.includes("pg") ||
    message.includes("postgres") ||
    message.includes("connect econnrefused") ||
    ("code" in err && typeof (err as { code?: string }).code === "string" && (err as { code: string }).code.startsWith("23"))
  ) {
    return ErrorCategory.DATABASE_ERROR;
  }

  if (name.includes("redis") || message.includes("redis")) {
    return ErrorCategory.REDIS_ERROR;
  }

  if (name === "validationerror" || message.includes("validation")) {
    return ErrorCategory.VALIDATION_ERROR;
  }

  return ErrorCategory.API_ERROR;
}
