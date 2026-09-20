"""Lapis client (ADR-0002, ADR-0003; Wiki Format principle 10): every
write is read-modify-write against a base revision. A conflict is never
resolved by overwriting - it produces a conflict note under
`.sync-conflicts/`, never a silent clobber. `LocalDirClient` gives tests
and demos the same read/write/base-revision semantics without a network
(Spec, Testing Decisions, Seam 1) and is what Phase 1's pipeline tests
actually run against; `LapisClient`'s endpoint shapes are provisional
pending the real Lapis API contract.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx


@dataclass
class ReadResult:
    content: str
    revision: str


@dataclass
class WriteResult:
    revision: str


class ConflictError(Exception):
    def __init__(self, path: str, base_revision: str, current_revision: str):
        super().__init__(
            f"conflict writing {path}: base_revision={base_revision!r} != current={current_revision!r}"
        )
        self.path = path
        self.base_revision = base_revision
        self.current_revision = current_revision


class VaultClient(Protocol):
    async def read(self, path: str) -> ReadResult: ...
    async def write(self, path: str, content: str, base_revision: str) -> WriteResult: ...


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


class LocalDirClient:
    """A directory standing in for Lapis with identical read/write/
    base-revision semantics (Testing Decisions, Seam 1). Revision is a
    content hash; a stale base_revision always produces a conflict note
    and never clobbers what's on disk."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _file(self, path: str) -> Path:
        return self.root / path

    async def read(self, path: str) -> ReadResult:
        content = self._file(path).read_text()
        return ReadResult(content=content, revision=_hash(content))

    async def write(self, path: str, content: str, base_revision: str) -> WriteResult:
        file = self._file(path)
        if file.exists():
            current_content = file.read_text()
            current_revision = _hash(current_content)
            if base_revision != current_revision:
                self._write_conflict_note(path, base_revision, current_content, content)
                raise ConflictError(path, base_revision, current_revision)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        return WriteResult(revision=_hash(content))

    def _write_conflict_note(
        self, path: str, base_revision: str, current_content: str, attempted_content: str
    ) -> None:
        conflicts_dir = self.root / ".sync-conflicts"
        conflicts_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        note_path = conflicts_dir / f"{path.replace('/', '_')}.{ts}.md"
        note_path.write_text(
            f"# Sync conflict: {path}\n\n"
            f"Write attempted against stale base_revision {base_revision!r}; "
            f"current revision was {_hash(current_content)!r}. Nothing was overwritten.\n\n"
            f"## Current content (kept on disk)\n\n```\n{current_content}\n```\n\n"
            f"## Attempted content (discarded, recorded here for a human to reconcile)\n\n"
            f"```\n{attempted_content}\n```\n"
        )


class LapisClient:
    """Live client over the Lapis HTTP API."""

    def __init__(
        self,
        base_url: str,
        vault_id: str,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.vault_id = vault_id
        self.token = token
        self._client = client or httpx.AsyncClient(timeout=30)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def read(self, path: str) -> ReadResult:
        resp = await self._client.get(
            f"{self.base_url}/vault/{self.vault_id}/file/{path}", headers=self._headers()
        )
        resp.raise_for_status()
        data = resp.json()
        return ReadResult(content=data["content"], revision=data["revision"])

    async def write(self, path: str, content: str, base_revision: str) -> WriteResult:
        resp = await self._client.put(
            f"{self.base_url}/vault/{self.vault_id}/file/{path}",
            json={"content": content, "base_revision": base_revision},
            headers=self._headers(),
        )
        if resp.status_code == 409:
            data = resp.json()
            raise ConflictError(path, base_revision, data.get("current_revision", "unknown"))
        resp.raise_for_status()
        return WriteResult(revision=resp.json()["revision"])

    async def aclose(self) -> None:
        await self._client.aclose()
