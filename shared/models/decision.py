"""Decision Model client (ADR-0006: Jev, sub-second, batched, returns
probabilities rather than text). Jev is typesafe.ai's "System One"
model: three atomic question primitives - Choice, Score, Noul -
answered directly as structured probabilities instead of generated
text, via the `typesafe-sdk` package's `system_one()` call
(docs.typesafe.ai). `JevClient` wraps `AsyncTypeSafeClient` and adapts
its response shape to our own `ChoiceResult`/`NoulResult`/`ScoreResult`
(shared.models.schemas) so call sites don't care whether they're
talking to Jev or `WorkerBackedDecisionModel`.

Two real shape differences from Jev's own response worth noting:
- Jev's Noul answer is a single `noul: float` probability with no
  confidence stat at all ("Noul answers don't carry one" -
  docs.typesafe.ai/confidence). Our NoulResult still wants a
  `confidence` float (nothing else in this codebase reads it, but the
  type is shared with `FixtureDecisionModel`), so JevClient derives one
  as the probability's distance from 0.5, rescaled to 0-1 - a
  reasonable proxy, not something Jev computes itself.
- Jev's ChoiceAnswer has its own `confidence` field (a statistic over
  the whole probability shape, not just probabilities[choice]), but
  our `ChoiceResult.confidence` is a derived property
  (`probabilities.get(option)`) that many existing tests construct
  `ChoiceResult` around directly. JevClient passes through Jev's real
  probabilities dict as-is; the two confidence definitions usually
  agree closely enough in practice that reconciling them isn't worth
  breaking that established internal contract.
`FixtureDecisionModel` is what Phase 1-4's tests actually run against
(Testing Decisions: "Decision Model responses as probability tables").
"""
from __future__ import annotations

import re
from typing import Protocol

from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score

from shared.models.calls import generate
from shared.models.schemas import ChoiceResult, NoulResult, ScoreResult
from shared.models.text import TextModelClient


class DecisionModelProtocol(Protocol):
    async def choice(self, question: str, options: dict[str, str | None]) -> ChoiceResult: ...
    async def noul(self, question: str) -> NoulResult: ...
    async def score(self, question: str) -> ScoreResult: ...


class JevClient:
    """Thin adapter over `typesafe_sdk.AsyncTypeSafeClient` - one
    `system_one()` call per question. Our Choice/Noul/Score questions
    are single self-contained strings (context + what to decide, both
    in one), so `question` is passed as both `state` and each
    question's `instructions` - redundant, but Noul's `instructions`
    (or `criteria`) is not optional server-side (confirmed live:
    `Noul()` with neither raises "Noul question must have criteria or
    instructions"), and passing it everywhere is simpler than reasoning
    about which question types can get away without it."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self._client = AsyncTypeSafeClient(api_key=api_key, base_url=base_url)

    async def choice(self, question: str, options: dict[str, str | None]) -> ChoiceResult:
        """`options` maps each option key to a human-readable
        description (Jev's `criteria`) - `None` is a legal value for an
        option needing no elaboration (e.g. "chatter"), but a bare list
        of opaque keys with no description at all gives Jev nothing to
        judge relevance against, which is why this takes a dict, not a
        list."""
        response = await self._client.system_one(
            state=question, questions={"result": Choice(instructions=question, criteria=options)}
        )
        answer = response.choices["result"]
        return ChoiceResult(option=answer.choice, probabilities=dict(answer.probabilities))

    async def noul(self, question: str) -> NoulResult:
        response = await self._client.system_one(
            state=question, questions={"result": Noul(instructions=question)}
        )
        probability = response.nouls["result"].noul
        return NoulResult(answer=probability >= 0.5, confidence=abs(probability - 0.5) * 2)

    async def score(self, question: str) -> ScoreResult:
        # Score needs an explicit rubric (criteria); nothing in this
        # codebase calls .score() yet, so there's no real rubric to
        # borrow - "low/medium/high" is a generic 3-rung placeholder,
        # not a verified default. Replace it with real criteria the
        # moment a caller needs Score for something specific.
        criteria = ["low", "medium", "high"]
        response = await self._client.system_one(
            state=question, questions={"result": Score(instructions=question, criteria=criteria)}
        )
        # Confirmed live: Jev's raw score is a rung index (0..len(criteria)-1,
        # possibly interpolated), NOT normalized to 0-1 as docs.typesafe.ai's
        # own quickstart example implies - a top-rung answer on this 3-item
        # scale came back as 2.0, not 1.0. Normalize here so ScoreResult.value
        # means the same thing (0-1) regardless of which DecisionModelProtocol
        # implementation produced it - WorkerBackedDecisionModel.score()
        # already returns a 0-1 float.
        return ScoreResult(value=response.scores["result"].score / (len(criteria) - 1))

    async def aclose(self) -> None:
        await self._client.aclose()


class WorkerBackedDecisionModel:
    """A real, functional DecisionModelProtocol for deployments with no
    Jev access: every Choice/Noul/Score question goes to the Worker
    model directly instead of a dedicated Decision Model. This is
    slower and more expensive than the v1 design intends (ADR-0006
    exists precisely to avoid this), but it's a genuine implementation,
    not a stub - swap in a real JevClient later without touching any
    call site, since both satisfy the same protocol.

    Confidence is always reported as 1.0: there's no meaningful
    second-guessing signal without a real Decision Model, so
    `decide_with_fallback` always accepts this class's answer outright
    rather than pointlessly asking the same Worker model a second time.
    """

    def __init__(self, worker_client: TextModelClient):
        self._worker = worker_client

    async def choice(self, question: str, options: dict[str, str | None]) -> ChoiceResult:
        option_lines = "\n".join(
            f"- {opt}: {desc}" if desc else f"- {opt}" for opt, desc in options.items()
        )
        prompt = (
            f"{question}\n\nOptions:\n{option_lines}\n\n"
            "Reply with only the option name exactly as written above."
        )
        result = await generate(self._worker, "worker", prompt)
        picked = result.text.strip()
        if picked not in options:
            picked = next(iter(options))
        return ChoiceResult(option=picked, probabilities={picked: 1.0})

    async def noul(self, question: str) -> NoulResult:
        prompt = f"{question}\n\nAnswer with exactly one word: yes or no."
        result = await generate(self._worker, "worker", prompt)
        answer = bool(re.search(r"\byes\b", result.text, re.IGNORECASE))
        return NoulResult(answer=answer, confidence=1.0)

    async def score(self, question: str) -> ScoreResult:
        prompt = f"{question}\n\nAnswer with only a number between 0 and 1."
        result = await generate(self._worker, "worker", prompt)
        match = re.search(r"(\d*\.?\d+)", result.text)
        value = min(max(float(match.group(1)), 0.0), 1.0) if match else 0.5
        return ScoreResult(value=value)


def default_decision_client(worker_client: TextModelClient) -> DecisionModelProtocol:
    """JevClient if JEV_API_KEY is configured, else the Worker-backed
    fallback - the same choice `shared.cms.interface.default_cms_client()`
    makes between a live client and a stand-in. `base_url` is only
    needed to point at a non-default Jev deployment (typesafe_sdk
    defaults to their hosted API and reads TYPESAFE_API_KEY itself, but
    we pass api_key explicitly so JEV_API_KEY is the one source of
    truth for it, same as every other credential in .env)."""
    from shared.config import settings

    if settings.jev_api_key:
        return JevClient(api_key=settings.jev_api_key, base_url=settings.jev_base_url or None)
    return WorkerBackedDecisionModel(worker_client)


class FixtureDecisionModel:
    """Recorded probability tables, keyed by exact question text. A
    missing key is a test-authoring bug, not a silent default - it
    raises, same principle as lint never guessing at a bad value."""

    def __init__(
        self,
        choices: dict[str, ChoiceResult] | None = None,
        nouls: dict[str, NoulResult] | None = None,
        scores: dict[str, ScoreResult] | None = None,
    ):
        self._choices = choices or {}
        self._nouls = nouls or {}
        self._scores = scores or {}

    async def choice(self, question: str, options: dict[str, str | None]) -> ChoiceResult:
        if question not in self._choices:
            raise KeyError(f"no fixture Choice response recorded for {question!r}")
        return self._choices[question]

    async def noul(self, question: str) -> NoulResult:
        if question not in self._nouls:
            raise KeyError(f"no fixture Noul response recorded for {question!r}")
        return self._nouls[question]

    async def score(self, question: str) -> ScoreResult:
        if question not in self._scores:
            raise KeyError(f"no fixture Score response recorded for {question!r}")
        return self._scores[question]
