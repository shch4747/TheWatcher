"""
local_lapis_client.py — filesystem-backed stand-in for a real Lapis
client. Lets you test lapis_feedback_store.py (and anything else
that talks to Lapis) without the real Obsidian vault or any network
access.

Same two-method interface a real client needs to expose:
    read_file(path) -> str | None
    write_file(path, content) -> None

If your project already has a real Lapis client somewhere (from the
context_store.py side of things), use that instead once it's ready
— just make sure it exposes these same two methods, or adjust the
calls in lapis_feedback_store.py to match its actual method names.
"""
from pathlib import Path
from typing import Optional


class LocalFileLapisClient:
    def __init__(self, root: str = "./lapis_vault"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _full_path(self, path: str) -> Path:
        return self.root / path

    def read_file(self, path: str) -> Optional[str]:
        full = self._full_path(path)
        if not full.exists():
            return None
        return full.read_text(encoding="utf-8")

    def write_file(self, path: str, content: str) -> None:
        full = self._full_path(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")