-- =====================================================================
-- INTENT CLUSTERS (Phase 1 — Passive/Shadow Language Brain)
--
-- docs/LANGUAGE_INTELLIGENCE.md's Phase 1 step 2: "Add an embedding
-- column (pgvector) to a NEW intent-cluster table -- NOT a schema
-- change to language_experiences itself; that table stays a pure
-- interaction log." This is that new table.
--
-- One row = one example message known to belong to a given intent,
-- embedded with the same provider/model as the rest of the codebase
-- (app/ai/embedding_service.py, text-embedding-3-small, 1536 dims --
-- same vector(1536) convention as knowledge_chunks/customer_memories
-- in 003_knowledge_rag.sql). The v1 own Language Brain
-- (app/language/shadow_brain.py) is a plain nearest-neighbor lookup
-- against this table: "given a normalized message, return the closest
-- known intent cluster + a similarity score." No fine-tuning, no
-- custom model training -- that is explicitly out of scope for Phase 1.
--
-- Tenant isolation: every row requires tenant_id (NOT NULL, FK'd) and
-- every query against this table MUST filter by tenant_id, same as
-- every other tenant-scoped table in this codebase.
--
-- `source` distinguishes how a row got here. Today only
-- 'default_seed' exists (app/language/shadow_brain.py bootstraps a
-- small generic example set the first time a tenant has zero rows,
-- so the matcher has something to compare against before any real
-- ingestion pipeline exists). 'tenant_provided'/'promoted' are placeholders
-- for later phases (a tenant curating their own examples; Phase 3's
-- automated learning/promotion pipeline promoting real
-- language_experiences rows into new clusters) -- nothing writes
-- those values yet.
--
-- Shadow-only: nothing in the Kernel reads this table. It is only
-- ever read from app/language/shadow_brain.py, called from
-- app/agent/core_agent.py strictly AFTER the Kernel has already
-- produced its real, LLM-driven reply -- so this table existing, or a
-- query against it failing, can never change what the customer
-- receives. See docs/LANGUAGE_INTELLIGENCE.md's Phase 1 section.
-- =====================================================================

CREATE TABLE intent_clusters (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    intent              VARCHAR(100) NOT NULL,
    example_message     TEXT NOT NULL,
    embedding           vector(1536) NOT NULL,
    source              VARCHAR(20) NOT NULL DEFAULT 'default_seed',
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_intent_clusters_tenant ON intent_clusters(tenant_id);
CREATE INDEX idx_intent_clusters_tenant_intent ON intent_clusters(tenant_id, intent);
-- Approximate nearest-neighbor index, same convention as
-- idx_knowledge_chunks_embedding in 003_knowledge_rag.sql. Run
-- `ANALYZE intent_clusters;` after seeding a meaningful number of rows
-- for a good query plan.
CREATE INDEX idx_intent_clusters_embedding ON intent_clusters
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
