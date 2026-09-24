-- =====================================================================
-- CUSTOMER-KNOWLEDGE-RAG-API-001
--
-- Extends the EXISTING knowledge_documents/knowledge_chunks tables
-- (003_knowledge_rag.sql) with an ingestion lifecycle, rather than
-- creating a second/competing set of tables. Adds product_categories
-- (genuinely new -- nothing like it existed) and knowledge_faqs
-- (structured Q&A, distinct from unstructured document chunks but
-- still tenant-scoped knowledge).
-- =====================================================================

-- ---------------------------------------------------------------------
-- Knowledge ingestion lifecycle (docs/ROADMAP.md item 2: "Build a
-- knowledge ingestion pipeline (upload -> clean -> chunk -> embed ->
-- store)"). knowledge_documents previously had no status/upload
-- metadata at all -- nothing had ever inserted a row into it.
-- ---------------------------------------------------------------------
ALTER TABLE knowledge_documents
    ADD COLUMN filename      VARCHAR(255),
    ADD COLUMN mime_type     VARCHAR(100),
    ADD COLUMN size_bytes    INTEGER,
    ADD COLUMN storage_path  VARCHAR(500),     -- node-api's local/object storage key; NULL for manually-entered (no file) documents
    ADD COLUMN status        VARCHAR(20) NOT NULL DEFAULT 'UPLOADED',
    ADD COLUMN error_message TEXT,             -- safe-to-display summary only; never a stack trace (see knowledge/ingestion_service.py)
    ADD COLUMN chunk_count   INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN processed_at  TIMESTAMP;

ALTER TABLE knowledge_documents
    ADD CONSTRAINT chk_knowledge_documents_status
    CHECK (status IN ('UPLOADED', 'PROCESSING', 'EXTRACTED', 'CHUNKED', 'EMBEDDING', 'READY', 'FAILED'));

CREATE INDEX idx_knowledge_documents_tenant_status ON knowledge_documents(tenant_id, status);

-- `source` was already on knowledge_documents (filename/URL/manual);
-- denormalized onto each chunk too so a chunk's provenance survives a
-- join-free read (task requirement: every chunk must be able to
-- identify tenant_id, document_id, category, language, source, chunk
-- index -- all now present directly on knowledge_chunks).
ALTER TABLE knowledge_chunks
    ADD COLUMN source VARCHAR(255);

-- ---------------------------------------------------------------------
-- Product categories (genuinely new -- no prior table/column for this)
-- ---------------------------------------------------------------------
CREATE TABLE product_categories (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name            VARCHAR(200) NOT NULL,
    slug            VARCHAR(100) NOT NULL,
    description     TEXT,
    parent_id       INTEGER REFERENCES product_categories(id) ON DELETE SET NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(tenant_id, slug)
);
CREATE INDEX idx_product_categories_tenant ON product_categories(tenant_id);
CREATE INDEX idx_product_categories_tenant_parent ON product_categories(tenant_id, parent_id);

-- Additive: existing `products` rows simply get category_id = NULL
-- (uncategorized); GET /products and POST /products both keep working
-- unmodified for callers that don't pass a category.
ALTER TABLE products
    ADD COLUMN category_id INTEGER REFERENCES product_categories(id) ON DELETE SET NULL;
CREATE INDEX idx_products_tenant_category ON products(tenant_id, category_id);

-- ---------------------------------------------------------------------
-- FAQ -- structured Q&A, distinct from uploaded/chunked documents but
-- still surfaced to RAG (see python-api/app/knowledge/ingestion_service.py
-- upsert_faq_chunk -- each FAQ is embedded as a single chunk, keyed by
-- a stable synthetic document so re-saving an FAQ replaces, not
-- duplicates, its chunk).
-- ---------------------------------------------------------------------
CREATE TABLE knowledge_faqs (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    category        VARCHAR(100),
    question        TEXT NOT NULL,
    answer          TEXT NOT NULL,
    -- Set once the question+answer has been embedded into
    -- knowledge_chunks (via a per-FAQ synthetic knowledge_documents
    -- row -- see ingestion_service.py); NULL until then/after an edit.
    knowledge_document_id INTEGER REFERENCES knowledge_documents(id) ON DELETE SET NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_knowledge_faqs_tenant ON knowledge_faqs(tenant_id);
