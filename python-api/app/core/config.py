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


settings = Settings()
