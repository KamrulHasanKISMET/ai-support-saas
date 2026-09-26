import json

from anthropic import AsyncAnthropic

from app.core.config import settings
from app.core.logging import logger


class AIService:
    """
    Single choke point for LLM calls. The Kernel, Intent Engine, and
    Memory Extraction all go through here — never call the provider
    SDK directly from elsewhere, so the model/provider can be swapped
    in one place (architecture doc section 42, "model-agnostic").
    """

    def __init__(self):
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        self.model = settings.llm_model

    async def complete(self, prompt: str, system: str | None = None, *, usage_out: dict[str, int | None] | None = None) -> str:
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system or "",
            messages=[{"role": "user", "content": prompt}],
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            logger.info(
                "LLM call model=%s input_tokens=%s output_tokens=%s",
                self.model,
                getattr(usage, "input_tokens", "?"),
                getattr(usage, "output_tokens", "?"),
            )
        if usage_out is not None:
            usage_out["input_tokens"] = getattr(usage, "input_tokens", None) if usage else None
            usage_out["output_tokens"] = getattr(usage, "output_tokens", None) if usage else None
        return "".join(
            block.text for block in response.content if block.type == "text"
        )

    async def complete_json(self, prompt: str, system: str | None = None, *, usage_out: dict[str, int | None] | None = None) -> dict:
        """Calls the LLM and parses a strict-JSON response (used by the
        Intent Engine and Memory Extraction). Falls back to an empty
        dict on parse failure rather than crashing the Kernel."""
        raw = await self.complete(prompt, system=system, usage_out=usage_out)
        try:
            cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
            return json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("Failed to parse LLM JSON response: %s", raw[:200])
            return {}


ai_service = AIService()
