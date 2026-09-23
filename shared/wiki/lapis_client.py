"""Lapis client (ADR-0002, ADR-0003; Wiki Format principle 10): every
write is read-modify-write against a base revision. A conflict is never
resolved by overwriting - it produces a conflict note under
`.sync-conflicts/`, never a silent clobber. `LocalDirClient` gives tests
and demos the same read/write/base-revision semantics without a network
(Spec, Testing Decisions, Seam 1) and is what Phase 1's pipeline tests
actually run against.

`LapisClient` talks to Lapis's real programmatic interface: a
Streamable HTTP MCP server at `{base_url}/api/mcp`, authenticated with
a personal access token (`Authorization: Bearer lapis_...`), confirmed
against ~/projects/lapis's `docs/architecture.md` and
`worker/src/mcp/server.ts`. There is no plain REST file API - every
operation is an MCP tool call (`read`, `write`, `edit`, `ls`, `find`,
`rm`, ...), and Lapis's revision is a non-negative **integer** per
file, not a content hash - `LocalDirClient` keeps its own string-hash
revision scheme since it's just a test double and the two never have to
agree with each other, only be internally consistent.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


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


class PageNotFound(LookupError):
    """`read()` of a path that doesn't exist. Every adapter raises this and
    only this for a missing page - a transport failure is never mistaken
    for "doesn't exist yet, go ahead and create it"."""

    def __init__(self, path: str):
        super().__init__(f"no page at {path}")
        self.path = path


class PageExists(Exception):
    """`create()` of a path that already exists. Nothing was written."""

    def __init__(self, path: str):
        super().__init__(f"page already exists at {path}")
        self.path = path


class VaultClient(Protocol):
    """The wiki store. Every adapter honours the same contract (and
    `tests/test_vault_contract.py` runs the same suite against each):

    - `read` returns the *whole* page, or raises `PageNotFound`.
    - `create` writes a page that must not exist yet, or raises
      `PageExists` and writes nothing.
    - `write` replaces an existing page read at `base_revision` (never
      empty - use `create` for a new page); a stale revision raises
      `ConflictError` and never overwrites.
    - `list` returns every `.md` path under a prefix, conflict notes
      excluded. `delete` of a missing path is a no-op.
    """

    async def read(self, path: str) -> ReadResult: ...
    async def create(self, path: str, content: str) -> WriteResult: ...
    async def write(self, path: str, content: str, base_revision: str) -> WriteResult: ...
    async def list(self, prefix: str) -> list[str]: ...
    async def delete(self, path: str) -> None: ...


def _require_base_revision(path: str, base_revision: str) -> None:
    if not base_revision:
        raise ValueError(
            f"write({path!r}) needs the base_revision it was read at - use create() for a new page"
        )


async def read_if_exists(vault: VaultClient, path: str) -> ReadResult | None:
    """`read`, with a missing page as None instead of an exception."""
    try:
        return await vault.read(path)
    except PageNotFound:
        return None


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
        file = self._file(path)
        if not file.is_file():
            raise PageNotFound(path)
        content = file.read_text()
        return ReadResult(content=content, revision=_hash(content))

    async def create(self, path: str, content: str) -> WriteResult:
        file = self._file(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with file.open("x") as handle:  # exclusive: fails if it exists
                handle.write(content)
        except FileExistsError:
            raise PageExists(path) from None
        return WriteResult(revision=_hash(content))

    async def write(self, path: str, content: str, base_revision: str) -> WriteResult:
        _require_base_revision(path, base_revision)
        file = self._file(path)
        current_content = file.read_text() if file.is_file() else ""
        current_revision = _hash(current_content) if file.is_file() else ""
        if base_revision != current_revision:
            self._write_conflict_note(path, base_revision, current_content, content)
            raise ConflictError(path, base_revision, current_revision)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        return WriteResult(revision=_hash(content))

    async def list(self, prefix: str) -> list[str]:
        base = self._file(prefix)
        if not base.exists():
            return []
        return sorted(
            str(p.relative_to(self.root))
            for p in base.rglob("*.md")
            if ".sync-conflicts" not in p.parts
        )

    async def delete(self, path: str) -> None:
        file = self._file(path)
        if file.exists():
            file.unlink()

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


def default_vault_client() -> VaultClient:
    """LapisClient if LAPIS_TOKEN is configured, else a LocalDirClient
    rooted at VAULT_ROOT - the same live-vs-stand-in pattern as
    `shared.cms.interface.default_cms_client()`. Safe default for a
    fresh deploy: nothing writes to a real wiki until you explicitly
    configure Lapis credentials.

    The client is wrapped in `ObservedVaultClient` so every write and
    delete lands in the `obs_vault_ops` audit table (ADR-0013). Wrapping
    here rather than at the call sites is what makes the audit complete:
    every agent, command and job in the repo gets its vault client from
    this function. Tests that construct a `LocalDirClient` directly are
    deliberately left unwrapped.
    """
    from pathlib import Path

    from shared.config import settings

    # Imported here, not at module scope: shared.observability.vault
    # imports this module for the VaultClient protocol.
    from shared.observability.vault import ObservedVaultClient

    inner: VaultClient
    if settings.lapis_token:
        inner = LapisClient(settings.lapis_base_url, settings.lapis_vault_id, token=settings.lapis_token)
    else:
        inner = LocalDirClient(Path(settings.vault_root))
    return ObservedVaultClient(inner)


async def check_vault_connection(vault: VaultClient) -> dict:
    """Health check: can we actually reach and list the vault? A
    `LocalDirClient` always succeeds if its root directory exists;
    a `LapisClient` proves it can reach Lapis and list something,
    without requiring any specific file to exist."""
    try:
        paths = await vault.list("")
        return {"ok": True, "path_count": len(paths)}
    except Exception as e:  # noqa: BLE001 - health probe, never raises
        return {"ok": False, "error": str(e)}


_CONFLICT_RE = re.compile(r"server has (\d+), client base is (\d+)")
# Error texts from Lapis's worker/src/mcp/server.ts that mean something
# specific at our seam - everything else stays a generic McpToolError.
_NOT_FOUND_TEXTS = ("File not found", "Path not found")
_EXISTS_TEXT = "baseRevision is required when replacing an existing file"
_READ_PAGE_LINES = 2000  # the read tool's max `limit`
_FIND_PAGE_SIZE = 1000  # the find tool's max `limit`


class McpToolError(Exception):
    """A Lapis MCP tool call returned isError - anything other than a
    revision conflict (that's ConflictError instead)."""


def _raise_tool_error(name: str, arguments: dict[str, Any], text: str) -> None:
    """Map a Lapis tool's error text onto the seam's typed errors."""
    path = arguments.get("path", "")
    match = _CONFLICT_RE.search(text)
    if match:
        raise ConflictError(path, match.group(2), match.group(1))
    if any(t in text for t in _NOT_FOUND_TEXTS):
        raise PageNotFound(path)
    if _EXISTS_TEXT in text:
        raise PageExists(path)
    raise McpToolError(f"lapis mcp tool {name!r} failed: {text}")


class LapisClient:
    """Live client over Lapis's MCP server (`{base_url}/api/mcp`), not a
    REST API - Lapis has no other programmatic surface. Each call opens
    its own Streamable HTTP session; the server runs in stateless mode
    (`{ legacy: "stateless" }` per ~/projects/lapis's server.ts) so there
    is no session to keep warm across calls."""

    def __init__(self, base_url: str, vault_id: str, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.vault_id = vault_id
        self.token = token

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        arguments = {"vault": self.vault_id, **arguments}
        is_error, text = await self._transport(name, arguments)
        if is_error:
            _raise_tool_error(name, arguments, text)
        return json.loads(text) if text else {}

    async def _transport(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        """One MCP tool call -> (is_error, first text block). The only
        method that touches the network; tests replace it."""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with httpx2.AsyncClient(timeout=30, headers=headers) as http_client:
            async with streamable_http_client(
                f"{self.base_url}/api/mcp", http_client=http_client
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
        text = next((block.text for block in result.content if block.type == "text"), "")
        return bool(result.is_error), text

    async def read(self, path: str) -> ReadResult:
        """The read tool is line-paged (`offset`/`limit`, `truncated`);
        a long page - an append-only Timeline gets long - is fetched in
        pages, never silently cut at the first one and written back
        shorter."""
        lines: list[str] = []
        offset = 1
        revision = ""
        while True:
            data = await self._call_tool("read", {"path": path, "offset": offset, "limit": _READ_PAGE_LINES})
            if data.get("binary"):
                raise McpToolError(f"{path} is a binary file - LapisClient.read() only supports text")
            if revision and str(data["revision"]) != revision:
                offset, lines, revision = 1, [], ""  # changed between pages - start over
                continue
            revision = str(data["revision"])
            numbered = data["text"].split("\n") if data["text"] else []
            lines += [line.split("|", 1)[1] if "|" in line else line for line in numbered]
            if not data.get("truncated"):
                break
            offset = int(data["endLine"]) + 1
        return ReadResult(content="\n".join(lines), revision=revision)

    async def create(self, path: str, content: str) -> WriteResult:
        """No baseRevision: Lapis refuses that for a path that exists
        (mapped to PageExists in `_call_tool`), which is exactly
        create-if-absent."""
        data = await self._call_tool("write", {"path": path, "content": content})
        return WriteResult(revision=str(data["entry"]["revision"]))

    async def write(self, path: str, content: str, base_revision: str) -> WriteResult:
        """Lapis's `write` tool never raises for a stale base_revision -
        it 3-way-merges automatically when there's no real overlap, and
        only falls back to a conflict note (still returning ok:true,
        original content left in place) when the merge actually
        collides. `entry.conflict` is how that soft signal shows up; we
        turn it back into a raised ConflictError here so callers keep
        ADR-0002's "never a silent clobber, always visible" contract."""
        _require_base_revision(path, base_revision)
        arguments: dict[str, Any] = {"path": path, "content": content, "baseRevision": int(base_revision)}
        data = await self._call_tool("write", arguments)
        entry = data["entry"]
        conflict = entry.get("conflict")
        if conflict:
            raise ConflictError(
                path,
                str(conflict.get("clientBaseRevision", base_revision)),
                str(conflict.get("serverRevision", entry["revision"])),
            )
        return WriteResult(revision=str(entry["revision"]))

    async def list(self, prefix: str) -> list[str]:
        pattern = f"{prefix.rstrip('/')}/**" if prefix else "**"
        paths: list[str] = []
        offset = 0
        while True:
            arguments = {"pattern": pattern, "limit": _FIND_PAGE_SIZE, "offset": offset}
            data = await self._call_tool("find", arguments)
            page = data.get("paths", [])
            paths += page
            if not data.get("truncated") or not page:
                break
            offset += len(page)
        return [p for p in paths if p.endswith(".md") and ".sync-conflicts" not in p]

    async def delete(self, path: str) -> None:
        try:
            await self._call_tool("rm", {"path": path})
        except PageNotFound:
            pass
