"""Phase 1 Lapis client acceptance criteria (docs/Wiki Format.md
principle 10; Testing Decisions, Seam 1): stale base_revision is rejected
and never clobbers, conflicts surface to .sync-conflicts/, and the
local-dir adapter runs the same contract without a network."""
from __future__ import annotations

from pathlib import Path

import pytest
from shared.wiki.interface import ConflictError, LocalDirClient


@pytest.fixture
def vault(tmp_path: Path) -> LocalDirClient:
    return LocalDirClient(tmp_path)


async def test_write_then_read_round_trips(vault: LocalDirClient):
    write_result = await vault.write("projects/watcher.md", "hello", base_revision="")
    read_result = await vault.read("projects/watcher.md")
    assert read_result.content == "hello"
    assert read_result.revision == write_result.revision


async def test_second_write_with_correct_base_revision_succeeds(vault: LocalDirClient):
    first = await vault.write("projects/watcher.md", "v1", base_revision="")
    second = await vault.write("projects/watcher.md", "v2", base_revision=first.revision)
    assert (await vault.read("projects/watcher.md")).content == "v2"
    assert second.revision != first.revision


async def test_stale_base_revision_raises_and_never_clobbers(vault: LocalDirClient):
    await vault.write("projects/watcher.md", "v1", base_revision="")

    with pytest.raises(ConflictError):
        await vault.write("projects/watcher.md", "attempted-v2", base_revision="stale-revision")

    # the on-disk content is untouched - never a silent overwrite.
    assert (await vault.read("projects/watcher.md")).content == "v1"


async def test_conflict_writes_a_sync_conflict_note(vault: LocalDirClient, tmp_path: Path):
    await vault.write("projects/watcher.md", "v1", base_revision="")
    with pytest.raises(ConflictError):
        await vault.write("projects/watcher.md", "attempted-v2", base_revision="stale-revision")

    conflict_files = list((tmp_path / ".sync-conflicts").glob("projects_watcher.md.*"))
    assert len(conflict_files) == 1
    note = conflict_files[0].read_text()
    assert "v1" in note
    assert "attempted-v2" in note
    assert "stale-revision" in note


async def test_write_to_new_file_ignores_base_revision(vault: LocalDirClient):
    # nothing exists yet, so any base_revision (even garbage) is fine -
    # there's nothing to conflict with.
    result = await vault.write("new/page.md", "content", base_revision="doesnt-matter")
    assert (await vault.read("new/page.md")).content == "content"
    assert result.revision
