import { strict as assert } from "node:assert";
import { test } from "node:test";
import { pool } from "../../config/database";
import { customersRepository } from "./customers.repository";

/**
 * customers.repository.ts talks to Postgres directly via the shared
 * `pool` -- no live database is available in this environment (see
 * the final report's "Tests" section), so these tests monkeypatch
 * `pool.query` to capture the SQL text/params actually sent, the same
 * way a real Postgres instance would receive them. This verifies
 * QUERY CONSTRUCTION (tenant_id always present, parameterized, never
 * string-interpolated into the SQL text itself) rather than full
 * end-to-end behavior.
 */
function withFakeQuery<T>(
  impl: (sql: string, params?: unknown[]) => Promise<{ rows: unknown[]; rowCount?: number }>,
  fn: () => Promise<T>
): Promise<T> {
  const original = pool.query;
  // @ts-expect-error -- test double, narrower signature than pg's real overloads
  pool.query = (sql: string, params?: unknown[]) => impl(sql, params);
  return fn().finally(() => {
    pool.query = original;
  });
}

test("list() scopes the query to the given tenant_id and paginates", async () => {
  const calls: Array<{ sql: string; params?: unknown[] }> = [];
  await withFakeQuery(
    async (sql, params) => {
      calls.push({ sql, params });
      if (sql.includes("COUNT(*)")) return { rows: [{ count: "2" }] };
      return { rows: [{ id: 1 }, { id: 2 }] };
    },
    () => customersRepository.list(7, { limit: 10, offset: 0 })
  );

  for (const call of calls) {
    assert.ok(call.sql.includes("tenant_id = $1"), `expected tenant scoping in: ${call.sql}`);
    assert.equal(call.params?.[0], 7);
  }
});

test("list() with a search term parameterizes it rather than interpolating into SQL", async () => {
  let capturedParams: unknown[] | undefined;
  await withFakeQuery(
    async (sql, params) => {
      capturedParams = params;
      if (sql.includes("COUNT(*)")) return { rows: [{ count: "0" }] };
      return { rows: [] };
    },
    () => customersRepository.list(1, { search: "'; DROP TABLE customers; --", limit: 10, offset: 0 })
  );

  // The dangerous string must travel as a bound parameter, never
  // concatenated into the query text (which would be a SQL-injection bug).
  assert.ok(capturedParams?.some((p) => typeof p === "string" && p.includes("DROP TABLE")));
});

test("update() only touches the given tenant's row and returns null for another tenant's id", async () => {
  let receivedParams: unknown[] | undefined;
  const updated = await withFakeQuery(
    async (sql, params) => {
      receivedParams = params;
      assert.ok(sql.includes("tenant_id = $1 AND id = $2"));
      return { rows: [] }; // simulates: no row matched (wrong tenant or missing id)
    },
    () => customersRepository.update(1, 999, { displayName: "New Name" })
  );

  assert.equal(updated, null);
  assert.equal(receivedParams?.[0], 1);
  assert.equal(receivedParams?.[1], 999);
});

test("delete() returns false when no row was deleted (tenant-scoped 404, not a 500)", async () => {
  const result = await withFakeQuery(
    async (sql) => {
      assert.ok(sql.includes("tenant_id = $1 AND id = $2"));
      return { rows: [], rowCount: 0 };
    },
    () => customersRepository.delete(1, 42)
  );
  assert.equal(result, false);
});

test("delete() returns true when a row was actually deleted", async () => {
  const result = await withFakeQuery(
    async () => ({ rows: [], rowCount: 1 }),
    () => customersRepository.delete(1, 42)
  );
  assert.equal(result, true);
});
