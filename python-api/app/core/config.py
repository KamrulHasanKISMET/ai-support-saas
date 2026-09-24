from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Central configuration for the AI Service.
    Values load from environment variables / .env; see .env.example.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://saas_user:saas_password@localhost:5432/ai_support_saas"
    redis_url: str = "redis://localhost:6379/0"

    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-5"
    embedding_model: str = "text-embedding-3-small"
    openai_api_key: str = ""

    # Default Intent Engine confidence threshold (section 18).
    # Per-tenant overrides live in agent_configs.intent_confidence_min.
    intent_confidence_min: float = 0.60

    # Shared secret that node-api must present (as `x-internal-secret`)
    # on every call to this service. Nothing else should be able to
    # reach these routes — see app/core/security.py.
    internal_service_secret: str = ""

    # Context Engineering (app/context/context_engine.py): a rough
    # token budget for the assembled Memory + Knowledge block only —
    # NOT the full LLM prompt (system prompt + question are added
    # later, in kernel.py). Token count is ESTIMATED (len(text)//4),
    # not an exact tokenizer count — good enough to keep context from
    # growing unbounded without adding a new tokenizer dependency.
    context_token_budget: int = 2000

    # Production Reliability workstream (docs/RELIABILITY.md). Explicit,
    # tunable SQLAlchemy pool sizing instead of the library defaults
    # (pool_size=5, max_overflow=10, no recycle) — same idea as
    # node-api's DB_POOL_MAX/etc (config/env.ts). Keep the TOTAL
    # possible connections from both services under Postgres's own
    # max_connections; see docs/RELIABILITY.md "Scaling triggers".
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout_seconds: int = 30
    db_pool_recycle_seconds: int = 1800

    # Knowledge Base ingestion (CUSTOMER-KNOWLEDGE-RAG-API-001, phase 7).
    # Character-based, not token-based -- consistent with
    # context_token_budget's own len(text)//4 estimate above; avoids
    # pulling in a tokenizer dependency just for chunking. 1000 chars
    # (~250 tokens) with 150 overlap (~15%) is a reasonable default for
    # short-answer customer-support knowledge (FAQs, policy text); tune
    # per tenant later if needed, but no per-tenant override exists yet.
    knowledge_chunk_size_chars: int = 1000
    knowledge_chunk_overlap_chars: int = 150


settings = Settings()
