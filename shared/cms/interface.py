"""CMS interface (ADR-0009: member details live in the CMS, not the wiki).
`get_member` resolves a member by CMS id or wikilink title; results are
cached with a short TTL. `SeedFileCmsClient` reads the team CSV seed
(`~/projects/watcher-seed/members.json`, outside the vault) as the
bootstrap data source until the real CMS exposes protected read
endpoints - the live `CmsClient`'s endpoint shapes are provisional
pending that contract.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Protocol

import httpx

from shared.config import settings


@dataclass(frozen=True)
class MemberRecord:
    cms_id: str
    title: str
    role: str | None = None
    status: str | None = None


class CmsClientProtocol(Protocol):
    async def get_member(self, ref: str) -> MemberRecord | None: ...
    async def list_members(self) -> list[MemberRecord]: ...


class SeedFileCmsClient:
    """Bootstrap data source: a JSON file shaped like
    `~/projects/watcher-seed/members.json` - a list of
    {id, title, role, status} objects."""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> list[MemberRecord]:
        data = json.loads(self.path.read_text())
        return [
            MemberRecord(cms_id=str(m["id"]), title=m["title"], role=m.get("role"), status=m.get("status"))
            for m in data
        ]

    async def list_members(self) -> list[MemberRecord]:
        return self._load()

    async def get_member(self, ref: str) -> MemberRecord | None:
        for m in self._load():
            if m.cms_id == ref or m.title == ref:
                return m
        return None


class CmsClient:
    """Live client over the ARIES CMS's protected read endpoints."""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        cache_ttl_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.cache_ttl_s = cache_ttl_s
        self._client = client or httpx.AsyncClient(timeout=15)
        self._cache: dict[str, tuple[float, MemberRecord | None]] = {}

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def get_member(self, ref: str) -> MemberRecord | None:
        cached = self._cache.get(ref)
        if cached is not None and time.monotonic() - cached[0] < self.cache_ttl_s:
            return cached[1]

        resp = await self._client.get(f"{self.base_url}/members/{ref}", headers=self._headers())
        if resp.status_code == 404:
            self._cache[ref] = (time.monotonic(), None)
            return None
        resp.raise_for_status()
        data = resp.json()
        member = MemberRecord(
            cms_id=str(data["id"]), title=data["title"], role=data.get("role"), status=data.get("status")
        )
        self._cache[ref] = (time.monotonic(), member)
        return member

    async def list_members(self) -> list[MemberRecord]:
        resp = await self._client.get(f"{self.base_url}/members", headers=self._headers())
        resp.raise_for_status()
        return [
            MemberRecord(cms_id=str(m["id"]), title=m["title"], role=m.get("role"), status=m.get("status"))
            for m in resp.json()
        ]

    async def aclose(self) -> None:
        await self._client.aclose()


def fuzzy_match_member(
    display_name: str, candidates: list[MemberRecord], threshold: float = 0.75
) -> MemberRecord | None:
    """Best member-title match for a WhatsApp display name (Spec: Gateway
    identity - "fuzzy match of display name against member titles"). No
    match below the threshold returns None so the caller can offer to
    create a member page instead of guessing."""
    best: MemberRecord | None = None
    best_score = 0.0
    for candidate in candidates:
        score = SequenceMatcher(None, display_name.lower(), candidate.title.lower()).ratio()
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= threshold else None


def default_cms_client() -> CmsClientProtocol:
    if settings.cms_base_url:
        return CmsClient(settings.cms_base_url, token=settings.cms_token)
    return SeedFileCmsClient(Path(settings.cms_seed_path).expanduser())
