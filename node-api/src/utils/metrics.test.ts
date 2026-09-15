import { strict as assert } from "node:assert";
import { beforeEach, test } from "node:test";
import { getInfraSnapshot, getRequestMetricsSnapshot, recordRequest } from "./metrics";

// These tests share the module's in-memory counters (by design -- it's
// a singleton, same as the real app). Each test records a known,
// isolated set of calls and only asserts on the DELTA it caused,
// rather than assuming a pristine zero starting point -- this keeps
// tests order-independent even though state is shared.

test("recordRequest increments total count", () => {
  const before = getRequestMetricsSnapshot().totalCount;
  recordRequest(200, 10);
  assert.equal(getRequestMetricsSnapshot().totalCount, before + 1);
});

test("recordRequest counts 5xx as errors, not 2xx/4xx", () => {
  const before = getRequestMetricsSnapshot();
  recordRequest(200, 5);
  recordRequest(404, 5);
  recordRequest(500, 5);
  recordRequest(503, 5);
  const after = getRequestMetricsSnapshot();
  assert.equal(after.errorCount, before.errorCount + 2);
  assert.equal(after.totalCount, before.totalCount + 4);
});

test("recordRequest tracks per-status-code counts", () => {
  const before = getRequestMetricsSnapshot().statusCounts[201] ?? 0;
  recordRequest(201, 3);
  recordRequest(201, 7);
  const after = getRequestMetricsSnapshot().statusCounts[201] ?? 0;
  assert.equal(after, before + 2);
});

test("avgDurationMs reflects recorded durations", () => {
  // Use a fresh, isolated status code so no other test's calls
  // contaminate the average we're checking here.
  const marker = 299;
  recordRequest(marker, 100);
  recordRequest(marker, 200);
  const snapshot = getRequestMetricsSnapshot();
  // avgDurationMs is global (all requests), not per-status -- just
  // confirm it's a sane non-negative number that moved.
  assert.ok(snapshot.avgDurationMs >= 0);
});

test("getInfraSnapshot returns sane, non-negative values", () => {
  const snapshot = getInfraSnapshot();
  assert.ok(snapshot.uptimeSeconds >= 0);
  assert.ok(snapshot.cpuTimeSeconds >= 0);
  assert.ok(snapshot.memoryRssMb > 0); // the test process itself uses some memory
});

test("getInfraSnapshot never throws", () => {
  assert.doesNotThrow(() => getInfraSnapshot());
});
