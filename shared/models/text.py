"""Worker/Mentor adapters: Pydantic AI agents behind one interface
(ADR-0005, ADR-0006). Tiers are swappable per config; a Worker call that
needs escalation (low Decision Model confidence, or a high-stakes item
like a decision) goes to the Mentor instead, same interface either way.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.output import PromptedOutput
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from shared.config import settings
from shared.models.schemas import StructuredResult, TextResult
from shared.observability.interface import resolve_cost

# How many times pydantic-ai re-asks the model after a reply that fails
# schema validation (or an output_validator's ModelRetry) before the run
# raises - the structured ingestion prompts are strict about ids, and a
# cheap Worker model does occasionally fumble one on the first try.
STRUCTURED_RETRIES = 2


class TextModelClient:
    """Wraps one Pydantic AI Agent. `model` is injected so tests can pass
    `pydantic_ai.models.test.TestModel()` instead of a live provider
    (Testing Decisions: "Worker/Mentor outputs as stored text")."""

    def __init__(
        self, model: Model | str, model_name: str, model_settings: ModelSettings | None = None
    ):
        self._model = model
        self.model_settings = model_settings
        self._agent: Agent[None, str] = Agent(
            model, output_type=str, retries=STRUCTURED_RETRIES, model_settings=model_settings
        )
        self.model_name = model_name

    async def generate(self, prompt: str, system: str | None = None) -> TextResult:
        result = await self._agent.run(prompt, instructions=system)
        usage = result.usage
        cost, cost_source = resolve_cost(usage, self.model_name, settings.models_base_url)
        return TextResult(
            text=result.output,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cost=cost,
            cost_source=cost_source,
        )

    async def generate_with_tools(
        self,
        prompt: str,
        *,
        tools: Sequence[Any],
        deps: Any,
        deps_type: type[Any] = object,
        system: str | None = None,
    ) -> TextResult:
        """Same as `generate`, but the model may call `tools`. Used by the Chat
        Agent's read-only wiki lookup. Ingestion stays on `generate` /
        `generate_structured` (no tools)."""
        agent: Agent[Any, str] = Agent(
            self._model,
            output_type=str,
            deps_type=deps_type,
            tools=list(tools),
            retries=STRUCTURED_RETRIES,
            model_settings=self.model_settings,
        )
        result = await agent.run(prompt, deps=deps, instructions=system)
        usage = result.usage
        cost, cost_source = resolve_cost(usage, self.model_name, settings.models_base_url)
        return TextResult(
            text=result.output,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cost=cost,
            cost_source=cost_source,
        )

    async def generate_structured[OutputT: BaseModel](
        self, prompt: str, output_type: type[OutputT], system: str | None = None
    ) -> StructuredResult[OutputT]:
        """Same agent, but the reply is parsed into `output_type`.
        `PromptedOutput` puts the JSON schema in the prompt and parses the
        text reply - no tool-calling support needed from the provider,
        which matters for the cheap OpenRouter Worker models. Validation
        failures are retried by pydantic-ai up to STRUCTURED_RETRIES."""
        result = await self._agent.run(
            prompt, output_type=PromptedOutput(output_type), instructions=system
        )
        usage = result.usage
        cost, cost_source = resolve_cost(usage, self.model_name, settings.models_base_url)
        return StructuredResult(
            output=result.output,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cost=cost,
            cost_source=cost_source,
        )


def openrouter_model(model_name: str) -> OpenAIChatModel:
    provider = OpenAIProvider(base_url=settings.models_base_url, api_key=settings.models_api_key)
    return OpenAIChatModel(model_name, provider=provider)


def client_for_model(
    model_name: str, model_settings: ModelSettings | None = None
) -> TextModelClient:
    """Any OpenRouter-style model id -> a ready TextModelClient. Used by
    the Phase 6 benchmark script to try several candidates side by side."""
    return TextModelClient(openrouter_model(model_name), model_name, model_settings=model_settings)


def glm_low_effort_settings(model_name: str) -> ModelSettings | None:
    """GLM 5.3 cannot turn reasoning off. `low` is the smallest effort
    OpenRouter accepts; anything else on that family is left alone."""
    if model_name.startswith("z-ai/glm-5.3"):
        return {"extra_body": {"reasoning": {"effort": "low"}}}
    return None


def deepseek_reasoning_off_settings(model_name: str) -> ModelSettings | None:
    """DeepSeek V4 Flash reasons unless the request disables it. The
    assignment default is that model; an override that is not it does
    not get the flag (GLM rejects `enabled: false`)."""
    if model_name.startswith("deepseek/deepseek-v4-flash"):
        return {"extra_body": {"reasoning": {"enabled": False}}}
    return None


def worker_model() -> TextModelClient:
    return client_for_model(settings.worker_model_name)


def wa_worker_model() -> TextModelClient:
    """The Worker the WhatsApp agent calls (chat and thread pages).
    GLM runs at low reasoning effort; other worker ids are unchanged."""
    name = settings.worker_model_name
    return client_for_model(name, glm_low_effort_settings(name))


def assignment_model() -> TextModelClient:
    """The model that assigns messages to threads. DeepSeek V4 Flash
    with reasoning off, unless `INGEST_MODEL_NAME` overrides it."""
    name = settings.ingest_model_name or settings.assignment_model_name
    return client_for_model(name, deepseek_reasoning_off_settings(name))


def mentor_model() -> TextModelClient:
    return client_for_model(settings.mentor_model_name)
