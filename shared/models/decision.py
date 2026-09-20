"""Decision Model client (ADR-0006: Jev, sub-second, batched, returns
probabilities rather than text). `JevClient`'s endpoint shapes are
provisional - a small in-house client per the Spec, not a public API
with a fixed contract we can verify against yet. `FixtureDecisionModel`
is what Phase 1-4's tests actually run against (Testing Decisions:
"Decision Model responses as probability tables").
"""
from __future__ import annotations

from typing import Protocol

import httpx

from shared.models.schemas import ChoiceResult, NoulResult, ScoreResult


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
