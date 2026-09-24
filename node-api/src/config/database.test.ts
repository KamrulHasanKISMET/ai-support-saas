import { strict as assert } from "node:assert";
import { test } from "node:test";
import { instrumentPoolQuery } from "./database";
import { registry } from "../observability/registry";

// instrumentPoolQuery is exercised against a plain fake `{ query }` object,
// not the real `pg.Pool` -- this is a unit test of the timing/labeling
// logic, not an integration test against a live Postgres (there isn't
// one in this test run). The real pool wired up in this file is covered
// end-to-end whenever any repository test / route test calls pool.query.

test("instrumentPoolQuery passes through a resolved value and records status=ok", async () => {
  const fake = {
    query: async (sql: string) => ({ rows: [{ sql }] }),
  };
  const wrapped = instrumentPoolQuery(fake);

  const result = await wrapped.query("select 1");
  assert.deepEqual(result, { rows: [{ sql: "select 1" }] });

  const body = await registry.metrics();
  assert.match(body, /db_query_duration_seconds_count\{status="ok"\} \d/);
});

test("instrumentPoolQuery re-throws a rejection and records status=error", async () => {
  const fake = {
    query: async () => {
      throw new Error("connection refused");
    },
  };
  const wrapped = instrumentPoolQuery(fake);

  await assert.rejects(() => wrapped.query("select 1"), /connection refused/);

  const body = await registry.metrics();
  assert.match(body, /db_query_duration_seconds_count\{status="error"\} \d/);
});
