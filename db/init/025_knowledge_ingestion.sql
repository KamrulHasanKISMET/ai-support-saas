-- =====================================================================
-- KNOWLEDGE INGESTION -- P6R-1 (docs/PHASE_5_8_PLAN.md, ROADMAP §2)
--
-- Adds what the ingestion pipeline (app/rag/ingestion.py) needs on top of
-- 003_knowledge_rag.sql, WITHOUT editing that old migration:
--   * content_sha256 : hash of the cleaned text, so re-uploading the same
--                      document for a tenant is a no-op, not a duplicate
--   * chunk_count    : how many chunks the document produced
--   * embedding_model: which embedding model produced the chunk vectors
--                      (vectors from different models are not comparable;
--                      a model change means re-ingest)
--
-- Tenant isolation: uniqueness is per (tenant_id, content_sha256) and
-- per (tenant_id, source) -- two tenants may hold identical text.
-- Existing databases: apply with scripts/migrate.sh 025 (idempotent).
-- =====================================================================

ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS content_sha256  CHAR(64);
ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS chunk_count     INTEGER NOT NULL DEFAULT 0;
ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS embedding_model VARCHAR(100);

CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_documents_tenant_hash
    ON knowledge_documents (tenant_id, content_sha256) WHERE content_sha256 IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_documents_tenant_source
    ON knowledge_documents (tenant_id, source) WHERE source IS NOT NULL;

-- Keyword search (rag/search.py) runs to_tsvector('simple', content) per
-- row; a GIN index makes that scale. Same expression as the query.
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_fts
    ON knowledge_chunks USING GIN (to_tsvector('simple', content));
