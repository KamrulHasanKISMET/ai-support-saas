import { strict as assert } from "node:assert";
import { test } from "node:test";
import { ErrorCategory, classifyError } from "./errorCategory";

test("classifies a timeout error by name", () => {
  const err = new Error("request failed");
  err.name = "TimeoutError";
  assert.equal(classifyError(err), ErrorCategory.TIMEOUT);
});

test("classifies a timeout error by message", () => {
  assert.equal(classifyError(new Error("Operation timeout after 30000ms")), ErrorCategory.TIMEOUT);
});

test("classifies ETIMEDOUT as timeout", () => {
  assert.equal(classifyError(new Error("connect ETIMEDOUT 1.2.3.4:5432")), ErrorCategory.TIMEOUT);
});

test("classifies a postgres connection refusal as a database error", () => {
  assert.equal(
    classifyError(new Error("connect ECONNREFUSED 127.0.0.1:5432")),
    ErrorCategory.DATABASE_ERROR
  );
});

test("classifies a pg unique-violation code (23505) as a database error", () => {
  const err = new Error("duplicate key value violates unique constraint") as Error & {
    code: string;
  };
  err.code = "23505";
  assert.equal(classifyError(err), ErrorCategory.DATABASE_ERROR);
});

test("classifies a redis error by message", () => {
  assert.equal(classifyError(new Error("Redis connection to localhost:6379 failed")), ErrorCategory.REDIS_ERROR);
});

test("classifies a validation error by name", () => {
  const err = new Error("tenantId is required");
  err.name = "ValidationError";
  assert.equal(classifyError(err), ErrorCategory.VALIDATION_ERROR);
});

test("falls back to api_error for an unrecognized error", () => {
  assert.equal(classifyError(new Error("something unexpected")), ErrorCategory.API_ERROR);
});

test("falls back to api_error for a non-Error thrown value, never throws itself", () => {
  assert.doesNotThrow(() => {
    assert.equal(classifyError("just a string"), ErrorCategory.API_ERROR);
    assert.equal(classifyError(null), ErrorCategory.API_ERROR);
    assert.equal(classifyError(undefined), ErrorCategory.API_ERROR);
    assert.equal(classifyError({ some: "object" }), ErrorCategory.API_ERROR);
  });
});

test("timeout check takes priority over a database-sounding message", () => {
  // e.g. a Postgres query that timed out -- should be TIMEOUT, not
  // DATABASE_ERROR, since "what actually went wrong" is more useful
  // than "which library raised it".
  assert.equal(
    classifyError(new Error("Postgres query timeout exceeded")),
    ErrorCategory.TIMEOUT
  );
});
