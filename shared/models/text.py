"""Worker/Mentor adapters: Pydantic AI agents behind one interface
(ADR-0005, ADR-0006). Tiers are swappable per config; a Worker call that
needs escalation (low Decision Model confidence, or a high-stakes item
like a decision) goes to the Mentor instead, same interface either way.
"""
from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from shared.config import settings
from shared.models.schemas import TextResult


class TextModelClient:
    """Wraps one Pydantic AI Agent. `model` is injected so tests can pass
    `pydantic_ai.models.test.TestModel()` instead of a live provider
    (Testing Decisions: "Worker/Mentor outputs as stored text")."""

    def __init__(self, model: Model | str, model_name: str):
        self._agent: Agent[None, str] = Agent(model, output_type=str)
        self.model_name = model_name

    async def generate(self, prompt: str, system: str | None = None) -> TextResult:
        result = await self._agent.run(prompt, instructions=system)
        usage = result.usage
        return TextResult(
            text=result.output,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
        )


def openrouter_model(model_name: str) -> OpenAIChatModel:
    provider = OpenAIProvider(base_url=settings.models_base_url, api_key=settings.models_api_key)
    return OpenAIChatModel(model_name, provider=provider)


def client_for_model(model_name: str) -> TextModelClient:
    """Any OpenRouter-style model id -> a ready TextModelClient. Used by
    the Phase 6 benchmark script to try several candidates side by side."""
    return TextModelClient(openrouter_model(model_name), model_name)


def worker_model() -> TextModelClient:
    return client_for_model(settings.worker_model_name)


def mentor_model() -> TextModelClient:
    return client_for_model(settings.mentor_model_name)
