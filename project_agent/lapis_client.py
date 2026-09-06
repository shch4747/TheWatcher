"""
Thin wrapper around the Lapis REST API (see 0xDevansh/lapis README, "API"
section). Handles reading/writing vault files, plus splitting a note into
its YAML frontmatter and markdown body, since the Project Agent almost
always wants to work with those separately.

Auth: uses a Bearer device token (LAPIS_DEVICE_TOKEN), not a browser
session cookie — this needs to be obtained once via whatever device-code
flow Lapis exposes for non-plugin clients. Confirm the exact issuance
process with the Lapis maintainer; this client just assumes a valid,
long-lived token is already available via config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import requests
import yaml

from config import LAPIS_BASE_URL, LAPIS_DEVICE_TOKEN, LAPIS_VAULT_ID

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


@dataclass
class LapisNote:
    path: str
    frontmatter: dict[str, Any]
    body: str

    def render(self) -> str:
        """Serialize back into a full markdown file with YAML frontmatter."""
        fm_yaml = yaml.safe_dump(
            self.frontmatter, sort_keys=False, allow_unicode=True
        ).strip()
        return f"---\n{fm_yaml}\n---\n\n{self.body.strip()}\n"


class LapisClient:
    def __init__(
        self,
        base_url: str = LAPIS_BASE_URL,
        vault_id: str = LAPIS_VAULT_ID,
        token: str = LAPIS_DEVICE_TOKEN,
    ):
        if not vault_id or not token:
            raise ValueError(
                "LapisClient requires LAPIS_VAULT_ID and LAPIS_DEVICE_TOKEN "
                "to be set (see config.py)."
            )
        self.base_url = base_url.rstrip("/")
        self.vault_id = vault_id
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    # -- low level -----------------------------------------------------

    def _url(self, suffix: str) -> str:
        return f"{self.base_url}/api/vaults/{self.vault_id}/{suffix.lstrip('/')}"

    def manifest(self) -> list[dict[str, Any]]:
        """Metadata for every file in the vault."""
        r = self.session.get(self._url("manifest"))
        r.raise_for_status()
        return r.json()

    def read_raw(self, path: str) -> str:
        r = self.session.get(self._url(f"files/{path}"))
        r.raise_for_status()
        return r.text

    def write_raw(self, path: str, content: str) -> None:
        r = self.session.put(self._url(f"files/{path}"), data=content.encode("utf-8"))
        r.raise_for_status()

    def delete(self, path: str) -> None:
        r = self.session.delete(self._url(f"files/{path}"))
        r.raise_for_status()

    def search(self, query: str) -> list[dict[str, Any]]:
        r = self.session.get(self._url("search"), params={"q": query})
        r.raise_for_status()
        return r.json()

    def backlinks(self, path: str) -> list[dict[str, Any]]:
        r = self.session.get(self._url("backlinks"), params={"path": path})
        r.raise_for_status()
        return r.json()

    # -- note-level helpers ---------------------------------------------

    def read_note(self, path: str) -> LapisNote:
        raw = self.read_raw(path)
        match = _FRONTMATTER_RE.match(raw)
        if not match:
            # No frontmatter yet — treat whole file as body.
            return LapisNote(path=path, frontmatter={}, body=raw)
        fm_text, body = match.groups()
        frontmatter = yaml.safe_load(fm_text) or {}
        return LapisNote(path=path, frontmatter=frontmatter, body=body)

    def write_note(self, note: LapisNote) -> None:
        self.write_raw(note.path, note.render())

    def list_project_paths(self, projects_root: str = "projects") -> list[str]:
        """All project index pages under projects/<name>/index.md (or
        projects/<name>.md, depending on how the vault is organized).
        Adjust the filter below to match the actual convention once the
        vault has a few real projects to check against.
        """
        files = self.manifest()
        return [
            f["path"]
            for f in files
            if f["path"].startswith(f"{projects_root}/")
            and f["path"].endswith((".md",))
            and f["path"].count("/") <= 2  # top-level project note, not a sub-note
        ]
