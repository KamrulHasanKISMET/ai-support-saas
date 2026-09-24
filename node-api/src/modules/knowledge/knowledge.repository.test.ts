import { strict as assert } from "node:assert";
import { test } from "node:test";
import { pool } from "../../config/database";
import { knowledgeRepository } from "./knowledge.repository";

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

test("deleteFaq() also deletes the FAQ's embedded RAG chunk (synthetic knowledge_documents row)", async () => {
  const calls: Array<{ sql: string; params?: unknown[] }> = [];
  const result = await withFakeQuery(
    async (sql, params) => {
      calls.push({ sql, params });
      if (sql.startsWith("DELETE FROM knowledge_faqs")) {
        return { rows: [{ knowledge_document_id: 77 }] };
      }
      return { rows: [], rowCount: 1 };
    },
    () => knowledgeRepository.deleteFaq(1, 10)
  );

  assert.equal(result, true);
  const documentDelete = calls.find((c) => c.sql.startsWith("DELETE FROM knowledge_documents"));
  assert.ok(documentDelete, "expected the FAQ's synthetic knowledge_documents row to also be deleted");
  assert.equal(documentDelete?.params?.[0], 1); // tenant-scoped
  assert.equal(documentDelete?.params?.[1], 77); // the FAQ's knowledge_document_id
});

test("deleteFaq() skips the second delete when the FAQ was never embedded", async () => {
  const calls: Array<{ sql: string; params?: unknown[] }> = [];
  await withFakeQuery(
    async (sql, params) => {
      calls.push({ sql, params });
      return { rows: [{ knowledge_document_id: null }] };
    },
    () => knowledgeRepository.deleteFaq(1, 10)
  );

  assert.equal(calls.filter((c) => c.sql.startsWith("DELETE FROM knowledge_documents")).length, 0);
});

test("deleteFaq() returns false for a nonexistent/other-tenant FAQ", async () => {
  const result = await withFakeQuery(
    async () => ({ rows: [] }),
    () => knowledgeRepository.deleteFaq(1, 999)
  );
  assert.equal(result, false);
});

test("list() documents query is tenant-scoped", async () => {
  let capturedSql = "";
  let capturedParams: unknown[] | undefined;
  await withFakeQuery(
    async (sql, params) => {
      capturedSql = sql;
      capturedParams = params;
      if (sql.includes("COUNT(*)")) return { rows: [{ count: "0" }] };
      return { rows: [] };
    },
    () => knowledgeRepository.list(4, { limit: 10, offset: 0 })
  );
  assert.ok(capturedSql.includes("tenant_id = $1"));
  assert.equal(capturedParams?.[0], 4);
});
