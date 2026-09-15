-- =====================================================================
-- KNOWLEDGE BASE / RAG TABLES
-- Vector retrieval MUST always filter by tenant_id (see queries in
-- python-api/app/rag/search.py).
-- =====================================================================

CREATE TABLE knowledge_documents (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    title           VARCHAR(255) NOT NULL,
    source          VARCHAR(255),           -- file name / URL / manual entry
    language        VARCHAR(10) DEFAULT 'bn',
    category        VARCHAR(100),
    raw_content     TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_knowledge_documents_tenant ON knowledge_documents(tenant_id);

-- 1536 dims matches common embedding models (e.g. text-embedding-3-small).
-- Adjust to match whichever embedding model app/ai/embedding_service.py uses.
CREATE TABLE knowledge_chunks (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_id     INTEGER NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    content         TEXT NOT NULL,
    category        VARCHAR(100),
    language        VARCHAR(10) DEFAULT 'bn',
    embedding       vector(1536),
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_knowledge_chunks_tenant ON knowledge_chunks(tenant_id);
CREATE INDEX idx_knowledge_chunks_document ON knowledge_chunks(document_id);
-- Approximate nearest-neighbor index for vector search (ivfflat).
-- Run `ANALYZE knowledge_chunks;` after bulk inserts for good query plans.
CREATE INDEX idx_knowledge_chunks_embedding ON knowledge_chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- =====================================================================
-- CUSTOMER MEMORY (section 14/15) — structured + optionally embedded
-- =====================================================================

CREATE TABLE customer_memories (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id     INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    memory_type     VARCHAR(50) NOT NULL,   -- short_term | long_term | semantic | decision | episodic | business
    memory_key      VARCHAR(100) NOT NULL,
    memory_value    TEXT NOT NULL,
    confidence      DECIMAL(4,3),
    embedding       vector(1536),           -- nullable; only semantic memories get one
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_customer_memories_tenant_customer ON customer_memories(tenant_id, customer_id);
CREATE INDEX idx_customer_memories_embedding ON customer_memories
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- =====================================================================
-- CONVERSATION STATE (section 20)
-- =====================================================================

CREATE TABLE conversation_states (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    state_key       VARCHAR(100) NOT NULL,
    state_value     TEXT,
    confidence      DECIMAL(4,3),
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(conversation_id, state_key)
);
CREATE INDEX idx_conversation_states_tenant ON conversation_states(tenant_id);
