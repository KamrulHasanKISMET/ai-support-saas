-- Enable pgvector for embeddings (RAG + semantic memory)
CREATE EXTENSION IF NOT EXISTS vector;

-- Useful for gen_random_uuid() if we ever want UUID ids
CREATE EXTENSION IF NOT EXISTS pgcrypto;
