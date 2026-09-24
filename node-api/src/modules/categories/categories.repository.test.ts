import { strict as assert } from "node:assert";
import { test } from "node:test";
import { pool } from "../../config/database";
import { categoriesRepository } from "./categories.repository";

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

test("create() slugifies the name when no slug is given", async () => {
  let capturedParams: unknown[] | undefined;
  await withFakeQuery(
    async (_sql, params) => {
      capturedParams = params;
      return { rows: [{ id: 1, tenant_id: 1, name: "Winter Sale!", slug: "winter-sale" }] };
    },
    () => categoriesRepository.create(1, { name: "Winter Sale!" })
  );
  // [tenantId, name, slug, description, parentId]
  assert.equal(capturedParams?.[2], "winter-sale");
});

test("create() slugifies an explicitly given slug too, not just the name", async () => {
  let capturedParams: unknown[] | undefined;
  await withFakeQuery(
    async (_sql, params) => {
      capturedParams = params;
      return { rows: [{ id: 1 }] };
    },
    () => categoriesRepository.create(1, { name: "Sale", slug: "My Custom Slug!!" })
  );
  assert.equal(capturedParams?.[2], "my-custom-slug");
});

test("list() scopes to tenant_id", async () => {
  let capturedSql = "";
  let capturedParams: unknown[] | undefined;
  await withFakeQuery(
    async (sql, params) => {
      capturedSql = sql;
      capturedParams = params;
      return { rows: [] };
    },
    () => categoriesRepository.list(5)
  );
  assert.ok(capturedSql.includes("tenant_id = $1"));
  assert.equal(capturedParams?.[0], 5);
});

test("update() only patches provided fields, preserving the rest from the existing row", async () => {
  const existing = {
    id: 1,
    tenant_id: 1,
    name: "Old Name",
    slug: "old-name",
    description: "Old description",
    parent_id: null,
    created_at: "",
    updated_at: "",
  };
  let updateParams: unknown[] | undefined;
  await withFakeQuery(
    async (sql, params) => {
      if (sql.startsWith("SELECT")) return { rows: [existing] };
      updateParams = params;
      return { rows: [{ ...existing, name: "New Name" }] };
    },
    () => categoriesRepository.update(1, 1, { name: "New Name" })
  );

  // [tenantId, id, name, slug, description, parentId] -- slug/description
  // should carry over from `existing`, not be nulled out by the partial update.
  assert.equal(updateParams?.[2], "New Name");
  assert.equal(updateParams?.[3], "old-name");
  assert.equal(updateParams?.[4], "Old description");
});

test("update() returns null when the category doesn't exist for this tenant", async () => {
  const result = await withFakeQuery(
    async () => ({ rows: [] }), // findById finds nothing
    () => categoriesRepository.update(1, 999, { name: "X" })
  );
  assert.equal(result, null);
});

test("delete() is tenant-scoped and reports whether a row was actually removed", async () => {
  let capturedSql = "";
  const result = await withFakeQuery(
    async (sql) => {
      capturedSql = sql;
      return { rows: [], rowCount: 1 };
    },
    () => categoriesRepository.delete(3, 10)
  );
  assert.ok(capturedSql.includes("tenant_id = $1 AND id = $2"));
  assert.equal(result, true);
});
