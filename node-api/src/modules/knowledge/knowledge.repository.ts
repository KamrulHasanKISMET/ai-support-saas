import { pool } from "../../config/database";

export type KnowledgeStatus =
  | "UPLOADED"
  | "PROCESSING"
  | "EXTRACTED"
  | "CHUNKED"
  | "EMBEDDING"
  | "READY"
  | "FAILED";

export interface KnowledgeDocument {
  id: number;
  tenant_id: number;
  title: string;
  source: string | null;
  language: string | null;
  category: string | null;
  raw_content: string | null;
  filename: string | null;
  mime_type: string | null;
  size_bytes: number | null;
  storage_path: string | null;
  status: KnowledgeStatus;
  error_message: string | null;
  chunk_count: number;
  processed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeFaq {
  id: number;
  tenant_id: number;
  category: string | null;
  question: string;
  answer: string;
  knowledge_document_id: number | null;
  created_at: string;
  updated_at: string;
}

export const knowledgeRepository = {
  async createDocument(
    tenantId: number,
    data: {
      title: string;
      filename: string;
      mimeType: string;
      sizeBytes: number;
      storagePath: string;
      category?: string;
      language?: string;
    }
  ): Promise<KnowledgeDocument> {
    const { rows } = await pool.query<KnowledgeDocument>(
      `INSERT INTO knowledge_documents
         (tenant_id, title, source, language, category, filename, mime_type, size_bytes, storage_path, status)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'UPLOADED')
       RETURNING *`,
      [
        tenantId,
        data.title,
        data.filename,
        data.language ?? "bn",
        data.category ?? null,
        data.filename,
        data.mimeType,
        data.sizeBytes,
        data.storagePath,
      ]
    );
    return rows[0];
  },

  async list(
    tenantId: number,
    options: { status?: KnowledgeStatus; category?: string; limit: number; offset: number }
  ): Promise<{ documents: KnowledgeDocument[]; total: number }> {
    const conditions = ["tenant_id = $1"];
    const params: unknown[] = [tenantId];
    if (options.status) {
      params.push(options.status);
      conditions.push(`status = $${params.length}`);
    }
    if (options.category) {
      params.push(options.category);
      conditions.push(`category = $${params.length}`);
    }
    const where = conditions.join(" AND ");

    const countResult = await pool.query<{ count: string }>(
      `SELECT COUNT(*) FROM knowledge_documents WHERE ${where}`,
      params
    );

    params.push(options.limit, options.offset);
    const { rows } = await pool.query<KnowledgeDocument>(
      `SELECT * FROM knowledge_documents WHERE ${where}
       ORDER BY created_at DESC
       LIMIT $${params.length - 1} OFFSET $${params.length}`,
      params
    );

    return { documents: rows, total: Number(countResult.rows[0].count) };
  },

  async findById(tenantId: number, id: number): Promise<KnowledgeDocument | null> {
    const { rows } = await pool.query<KnowledgeDocument>(
      `SELECT * FROM knowledge_documents WHERE tenant_id = $1 AND id = $2`,
      [tenantId, id]
    );
    return rows[0] ?? null;
  },

  /** Returns the deleted row (so the route handler can also remove its
   * stored file via storage.ts) or null if it didn't exist/wasn't this
   * tenant's. `knowledge_chunks.document_id` is ON DELETE CASCADE (see
   * db/init/003_knowledge_rag.sql), so chunks/embeddings are cleaned up
   * by Postgres itself -- nothing extra to do on that side. */
  async deleteDocument(tenantId: number, id: number): Promise<KnowledgeDocument | null> {
    const { rows } = await pool.query<KnowledgeDocument>(
      `DELETE FROM knowledge_documents WHERE tenant_id = $1 AND id = $2 RETURNING *`,
      [tenantId, id]
    );
    return rows[0] ?? null;
  },

  /**
   * Used by the /process route to flip UPLOADED -> PROCESSING before
   * handing off to python-api, and as a fallback if python-api's
   * ingest call fails before it gets a chance to write FAILED itself
   * (e.g. network error, not a processing-logic error). Every other
   * status transition (EXTRACTED/CHUNKED/EMBEDDING/READY/FAILED with a
   * real chunk_count) is written by python-api directly — it owns the
   * actual pipeline and reports its own progress instead of Node
   * guessing at python-api's internal state.
   */
  async setStatus(
    tenantId: number,
    id: number,
    status: KnowledgeStatus,
    errorMessage?: string | null
  ): Promise<void> {
    await pool.query(
      `UPDATE knowledge_documents
          SET status = $3, error_message = $4, updated_at = now()
        WHERE tenant_id = $1 AND id = $2`,
      [tenantId, id, status, errorMessage ?? null]
    );
  },

  // ---- FAQ ----

  async listFaqs(tenantId: number, category?: string): Promise<KnowledgeFaq[]> {
    const { rows } = category
      ? await pool.query<KnowledgeFaq>(
          `SELECT * FROM knowledge_faqs WHERE tenant_id = $1 AND category = $2 ORDER BY created_at DESC`,
          [tenantId, category]
        )
      : await pool.query<KnowledgeFaq>(
          `SELECT * FROM knowledge_faqs WHERE tenant_id = $1 ORDER BY created_at DESC`,
          [tenantId]
        );
    return rows;
  },

  async createFaq(
    tenantId: number,
    data: { question: string; answer: string; category?: string }
  ): Promise<KnowledgeFaq> {
    const { rows } = await pool.query<KnowledgeFaq>(
      `INSERT INTO knowledge_faqs (tenant_id, category, question, answer)
       VALUES ($1, $2, $3, $4) RETURNING *`,
      [tenantId, data.category ?? null, data.question, data.answer]
    );
    return rows[0];
  },

  async findFaqById(tenantId: number, id: number): Promise<KnowledgeFaq | null> {
    const { rows } = await pool.query<KnowledgeFaq>(
      `SELECT * FROM knowledge_faqs WHERE tenant_id = $1 AND id = $2`,
      [tenantId, id]
    );
    return rows[0] ?? null;
  },

  async updateFaq(
    tenantId: number,
    id: number,
    data: { question?: string; answer?: string; category?: string }
  ): Promise<KnowledgeFaq | null> {
    const { rows } = await pool.query<KnowledgeFaq>(
      `UPDATE knowledge_faqs
          SET question = COALESCE($3, question),
              answer   = COALESCE($4, answer),
              category = COALESCE($5, category),
              -- an edited FAQ's old embedding no longer matches its
              -- text; unlink so /knowledge/faq/:id/process (or a future
              -- re-embed-on-save trigger) knows to regenerate it.
              knowledge_document_id = NULL,
              updated_at = now()
        WHERE tenant_id = $1 AND id = $2
        RETURNING *`,
      [tenantId, id, data.question ?? null, data.answer ?? null, data.category ?? null]
    );
    return rows[0] ?? null;
  },

  async setFaqKnowledgeDocumentId(tenantId: number, id: number, knowledgeDocumentId: number): Promise<void> {
    await pool.query(
      `UPDATE knowledge_faqs SET knowledge_document_id = $3, updated_at = now()
        WHERE tenant_id = $1 AND id = $2`,
      [tenantId, id, knowledgeDocumentId]
    );
  },

  /**
   * Deletes the FAQ AND its embedded RAG chunk (the synthetic
   * `knowledge_documents` row created by python-api's
   * upsert_faq_chunk — see knowledge_faqs.knowledge_document_id).
   * Without this second delete, a removed FAQ's answer would stay
   * forever searchable/retrievable by RAG even though it no longer
   * exists as an FAQ -- a real gap, not just a cosmetic one, since the
   * Kernel could keep citing a deleted policy. `knowledge_chunks` for
   * that document cascade-delete via its existing FK (see
   * db/init/003_knowledge_rag.sql), so deleting the one
   * knowledge_documents row is enough.
   */
  async deleteFaq(tenantId: number, id: number): Promise<boolean> {
    const { rows } = await pool.query<{ knowledge_document_id: number | null }>(
      `DELETE FROM knowledge_faqs WHERE tenant_id = $1 AND id = $2 RETURNING knowledge_document_id`,
      [tenantId, id]
    );
    if (rows.length === 0) return false;

    const knowledgeDocumentId = rows[0].knowledge_document_id;
    if (knowledgeDocumentId) {
      await pool.query(
        `DELETE FROM knowledge_documents WHERE tenant_id = $1 AND id = $2`,
        [tenantId, knowledgeDocumentId]
      );
    }
    return true;
  },
};
