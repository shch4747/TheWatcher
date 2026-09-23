"""The VaultClient contract (docs/Wiki Format.md principle 10, ADR-0002),
run against every adapter: `LocalDirClient`, and `LapisClient` over
`FakeLapis` - an in-memory stand-in for Lapis's MCP tools that follows
~/projects/lapis `worker/src/mcp/server.ts` (integer revisions,
line-numbered paged reads, `write` refusing an existing file without
`baseRevision`, a stale base answered with `entry.conflict` rather than
an error, "File not found" error text). Only `LapisClient._transport` is
replaced, so the adapter's own error mapping and paging are what's under
test. A test that passes on the local directory has to mean the same
thing in production."""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any

import pytest
from shared.wiki.interface import (
    ConflictError,
    LapisClient,
    LocalDirClient,
    McpToolError,
    PageExists,
    PageNotFound,
    VaultClient,
    read_if_exists,
)


class _ToolError(Exception):
    pass


class FakeLapis(LapisClient):
    """`max_read_lines` / `max_find` stand in for the server's paging
    limits, set small so paging is actually exercised."""

    def __init__(self, max_read_lines: int = 3, max_find: int = 2):
        super().__init__("http://lapis.invalid", "vault")
        self.files: dict[str, tuple[str, int]] = {}
        self.max_read_lines = max_read_lines
        self.max_find = max_find
        self.fail_next: str | None = None

    async def _transport(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        if self.fail_next:
            text, self.fail_next = self.fail_next, None
            return True, text
        arguments = {k: v for k, v in arguments.items() if k != "vault"}
        try:
            return False, json.dumps(getattr(self, f"_tool_{name}")(**arguments))
        except _ToolError as exc:
            return True, str(exc)

    def _tool_read(self, path: str, offset: int = 1, limit: int = 400) -> dict[str, Any]:
        if path not in self.files:
            raise _ToolError("File not found")
        content, revision = self.files[path]
        lines = content.split("\n")
        limit = min(limit, self.max_read_lines)
        end = min(len(lines), offset + limit - 1)
        body = [f"{offset + i}|{line}" for i, line in enumerate(lines[offset - 1 : end])]
        return {
            "path": path,
            "revision": revision,
            "text": "\n".join(body),
            "startLine": offset,
            "endLine": end,
            "totalLines": len(lines),
            "truncated": end < len(lines),
        }

    def _tool_write(self, path: str, content: str, baseRevision: int | None = None) -> dict[str, Any]:
        existing = self.files.get(path)
        if existing and baseRevision is None:
            raise _ToolError("baseRevision is required when replacing an existing file")
        if existing and baseRevision != existing[1]:
            conflict = {"clientBaseRevision": baseRevision, "serverRevision": existing[1]}
            return {"ok": True, "entry": {"path": path, "revision": existing[1], "conflict": conflict}}
        revision = (existing[1] + 1) if existing else 1
        self.files[path] = (content, revision)
        return {"ok": True, "entry": {"path": path, "revision": revision}}

    def _tool_find(self, pattern: str, limit: int = 1000, offset: int = 0) -> dict[str, Any]:
        matches = sorted(p for p in self.files if fnmatch.fnmatch(p, pattern.replace("**", "*")))
        limit = min(limit, self.max_find)
        return {"paths": matches[offset : offset + limit], "truncated": offset + limit < len(matches)}

    def _tool_rm(self, path: str) -> dict[str, Any]:
        if path not in self.files:
            raise _ToolError("File not found")
        del self.files[path]
        return {"ok": True, "path": path}


@pytest.fixture(params=["local", "lapis"])
def vault(request, tmp_path: Path) -> VaultClient:
    return LocalDirClient(tmp_path) if request.param == "local" else FakeLapis()


async def test_create_then_read_round_trips(vault: VaultClient):
    created = await vault.create("projects/watcher.md", "hello")
    read = await vault.read("projects/watcher.md")
    assert read.content == "hello"
    assert read.revision == created.revision


async def test_read_of_a_missing_page_raises_page_not_found(vault: VaultClient):
    with pytest.raises(PageNotFound):
        await vault.read("projects/nope.md")
    assert await read_if_exists(vault, "projects/nope.md") is None


async def test_create_refuses_an_existing_page_and_writes_nothing(vault: VaultClient):
    await vault.create("channels/demo.md", "human notes live here")
    with pytest.raises(PageExists):
        await vault.create("channels/demo.md", "fresh template")
    assert (await vault.read("channels/demo.md")).content == "human notes live here"


async def test_write_at_the_current_revision_succeeds(vault: VaultClient):
    first = await vault.create("projects/watcher.md", "v1")
    second = await vault.write("projects/watcher.md", "v2", base_revision=first.revision)
    assert (await vault.read("projects/watcher.md")).content == "v2"
    assert second.revision != first.revision


async def test_stale_base_revision_raises_and_never_clobbers(vault: VaultClient):
    first = await vault.create("projects/watcher.md", "v1")
    await vault.write("projects/watcher.md", "v2", base_revision=first.revision)
    with pytest.raises(ConflictError):
        await vault.write("projects/watcher.md", "attempted-v3", base_revision=first.revision)
    assert (await vault.read("projects/watcher.md")).content == "v2"


async def test_write_without_a_base_revision_is_refused(vault: VaultClient):
    with pytest.raises(ValueError, match="create"):
        await vault.write("projects/watcher.md", "v1", base_revision="")


async def test_long_pages_are_read_whole(vault: VaultClient):
    content = "\n".join(f"- line {i} | with a pipe" for i in range(25))
    await vault.create("channels/c/long.md", content)
    assert (await vault.read("channels/c/long.md")).content == content


async def test_list_returns_every_page_under_a_prefix(vault: VaultClient):
    for name in "abcde":
        await vault.create(f"channels/x/{name}.md", name)
    await vault.create("channels/other/z.md", "z")
    assert sorted(await vault.list("channels/x")) == [f"channels/x/{n}.md" for n in "abcde"]


async def test_delete_removes_and_is_a_no_op_when_missing(vault: VaultClient):
    await vault.create("channels/x/a.md", "a")
    await vault.delete("channels/x/a.md")
    await vault.delete("channels/x/a.md")
    assert await read_if_exists(vault, "channels/x/a.md") is None


async def test_a_transport_failure_is_not_mistaken_for_a_missing_page():
    lapis = FakeLapis()
    lapis.fail_next = "upstream 502"
    with pytest.raises(McpToolError):
        await read_if_exists(lapis, "channels/x.md")


async def test_local_conflict_writes_a_sync_conflict_note(tmp_path: Path):
    vault = LocalDirClient(tmp_path)
    await vault.create("projects/watcher.md", "v1")
    with pytest.raises(ConflictError):
        await vault.write("projects/watcher.md", "attempted-v2", base_revision="stale-revision")
    note = next((tmp_path / ".sync-conflicts").glob("projects_watcher.md.*")).read_text()
    assert "v1" in note and "attempted-v2" in note and "stale-revision" in note
