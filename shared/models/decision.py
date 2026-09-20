"""Decision Model client (ADR-0006: Jev, sub-second, batched, returns
probabilities rather than text). `JevClient`'s endpoint shapes are
provisional - a small in-house client per the Spec, not a public API
with a fixed contract we can verify against yet. `FixtureDecisionModel`
is what Phase 1-4's tests actually run against (Testing Decisions:
"Decision Model responses as probability tables").
"""
from __future__ import annotations

import re
from typing import Protocol

import httpx

from shared.models.calls import generate
from shared.models.schemas import ChoiceResult, NoulResult, ScoreResult
from shared.models.text import TextModelClient


class DecisionModelProtocol(Protocol):
    async def choice(self, question: str, options: list[str]) -> ChoiceResult: ...
    async def noul(self, question: str) -> NoulResult: ...
    async def score(self, question: str) -> ScoreResult: ...


class JevClient:
    def __init__(self, base_url: str, api_key: str | None = None, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=15)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def choice(self, question: str, options: list[str]) -> ChoiceResult:
        resp = await self._client.post(
            f"{self.base_url}/choice",
            json={"question": question, "options": options},
            headers=self._headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        return ChoiceResult(option=data["option"], probabilities=data["probabilities"])

    async def noul(self, question: str) -> NoulResult:
        resp = await self._client.post(
            f"{self.base_url}/noul", json={"question": question}, headers=self._headers()
        )
        resp.raise_for_status()
        data = resp.json()
        return NoulResult(answer=data["answer"], confidence=data["confidence"])

    async def score(self, question: str) -> ScoreResult:
        resp = await self._client.post(
            f"{self.base_url}/score", json={"question": question}, headers=self._headers()
        )
        resp.raise_for_status()
        return ScoreResult(value=resp.json()["value"])

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

    async def choice(self, question: str, options: list[str]) -> ChoiceResult:
        prompt = f"{question}\n\nPick exactly one of: {', '.join(options)}. Reply with only the option text."
        result = await generate(self._worker, "worker", prompt)
        picked = result.text.strip()
        if picked not in options:
            picked = options[0]
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
    """JevClient if JEV_BASE_URL is configured, else the Worker-backed
    fallback - the same choice `shared.cms.interface.default_cms_client()`
    makes between a live client and a stand-in."""
    from shared.config import settings

    if settings.jev_base_url:
        return JevClient(settings.jev_base_url, api_key=settings.jev_api_key)
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

    async def choice(self, question: str, options: list[str]) -> ChoiceResult:
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
