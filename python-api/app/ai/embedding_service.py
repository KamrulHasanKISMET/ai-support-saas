from openai import AsyncOpenAI

from app.core.config import settings
from app.core.logging import logger


class EmbeddingService:
    """
    Thin wrapper around whichever embedding provider is configured.
    Keeping this behind one interface means the provider (and vector
    dimension) can change without touching RAG/memory call sites --
    just keep the `vector(1536)` columns in sync if you change models.

    Uses OpenAI's text-embedding-3-small (1536 dimensions) by default,
    matching the `vector(1536)` columns already defined in
    db/init/003_knowledge_rag.sql. If you switch providers/models,
    update BOTH this file's dimension expectations AND the SQL column
    definitions (and re-run embeddings for any already-stored rows --
    changing dimension makes old embeddings incompatible).
    """

    def __init__(self, model: str = settings.embedding_model):
        self.model = model
        self._client: AsyncOpenAI | None = None

    @property
    def client(self) -> AsyncOpenAI:
        # Constructed on first use, not at import time -- an empty
        # OPENAI_API_KEY (e.g. a fresh local setup) must not crash the
        # whole python-api process on startup. It will still fail when
        # an embedding is actually requested, which the Kernel already
        # catches (see app/context/context_engine.py).
        if self._client is None:
            self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        return self._client

    async def embed(self, text: str) -> list[float]:
        """Returns an embedding vector for a single string."""
        vectors = await self.embed_batch([text])
        return vectors[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Embeds multiple strings in a single API call -- always prefer
        this over calling embed() in a loop when embedding more than
        one text (e.g. knowledge-chunk ingestion), since one batched
        call is far cheaper/faster than N individual calls.
        """
        if not texts:
            return []

        response = await self.client.embeddings.create(
            model=self.model,
            input=texts,
        )
        # OpenAI returns results in the same order as the input list.
        vectors = [item.embedding for item in response.data]

        logger.info(
            "Embedding call model=%s count=%d total_tokens=%s",
            self.model,
            len(texts),
            getattr(response.usage, "total_tokens", "?"),
        )
        return vectors


embedding_service = EmbeddingService()
