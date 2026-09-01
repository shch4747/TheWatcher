"""
Client over the Lapis REST API (https://github.com/0xDevansh/lapis).

Documented endpoints (all under /api/vaults/:id, Bearer-token auth):
  GET    /manifest            metadata for every file in the vault
  GET    /files/*             read a file by path        -> raw markdown
  PUT    /files/*             create or update a file
  DELETE /files/*             delete a file
  GET    /search?q=           full-text search with snippets
  GET    /backlinks?path=     notes linking to a path
  GET    /tags                all tags with counts

Config via env (same names the research-agent uses, so ONE .env serves both):
  LAPIS_BASE_URL       e.g. https://lapis.dvenom.in
  LAPIS_VAULT_ID       e.g. 6adc07d5-b530-462f-be6d-cc399288bb78
  LAPIS_BEARER_TOKEN   device Bearer token (device-code flow from the Lapis
                       Obsidian plugin) — preferred for an unattended agent
  LAPIS_SESSION_COOKIE alternative to the token (a browser session cookie;
                       expires, so not ideal for a cron)

`LapisClient` is the interface the adapter depends on; `HttpLapisClient` is
the real one; `FakeLapisClient` is an in-memory stand-in for tests (no
network, no token) so LapisAdapter is fully testable.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Optional


class LapisClient(ABC):
    @abstractmethod
    def read_file(self, path: str) -> Optional[str]:
        """Raw note content, or None if it doesn't exist."""

    @abstractmethod
    def write_file(self, path: str, content: str) -> None:
        """Create or update a note (PUT)."""

    @abstractmethod
    def delete_file(self, path: str) -> None: ...

    @abstractmethod
    def list_files(self, prefix: str = "") -> list[str]:
        """Paths of all notes whose path starts with `prefix`."""

    def search(self, q: str) -> list[dict[str, Any]]:  # optional, default empty
        return []


class HttpLapisClient(LapisClient):
    def __init__(self, base_url=None, vault_id=None, token=None, cookie=None, timeout=30):
        self.base_url = (base_url or os.environ["LAPIS_BASE_URL"]).rstrip("/")
        self.vault_id = vault_id or os.environ["LAPIS_VAULT_ID"]
        # LAPIS_BEARER_TOKEN preferred (matches research-agent); LAPIS_TOKEN
        # accepted as a fallback for older configs.
        self.token = (token if token is not None else
                      os.environ.get("LAPIS_BEARER_TOKEN") or os.environ.get("LAPIS_TOKEN", ""))
        self.cookie = cookie if cookie is not None else os.environ.get("LAPIS_SESSION_COOKIE", "")
        self.timeout = timeout

    def _url(self, suffix: str) -> str:
        return f"{self.base_url}/api/vaults/{self.vault_id}{suffix}"

    def _req(self, method: str, suffix: str, body: bytes | None = None,
             content_type: str = "text/markdown") -> tuple[int, bytes]:
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.cookie:
            headers["Cookie"] = self.cookie
        if body is not None:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self._url(suffix), data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, b""
            raise RuntimeError(f"lapis {method} {suffix} -> HTTP {e.code}: {e.read().decode(errors='replace')}") from e

    @staticmethod
    def _encode_path(path: str) -> str:
        return urllib.parse.quote(path)

    def read_file(self, path: str) -> Optional[str]:
        status, data = self._req("GET", f"/files/{self._encode_path(path)}")
        return None if status == 404 else data.decode("utf-8")

    def write_file(self, path: str, content: str) -> None:
        self._req("PUT", f"/files/{self._encode_path(path)}", body=content.encode("utf-8"))

    def delete_file(self, path: str) -> None:
        self._req("DELETE", f"/files/{self._encode_path(path)}")

    def list_files(self, prefix: str = "") -> list[str]:
        status, data = self._req("GET", "/manifest")
        if status == 404 or not data:
            return []
        manifest = json.loads(data.decode("utf-8"))
        # Manifest shape isn't nailed down in the docs — accept a list of
        # strings, a list of {path/name}, or {files:[...]}. Confirm against
        # the live vault; this is the one spot to adjust.
        entries = manifest.get("files", manifest) if isinstance(manifest, dict) else manifest
        paths = []
        for e in entries:
            p = e if isinstance(e, str) else (e.get("path") or e.get("name") or "")
            if p:
                paths.append(p)
        return [p for p in paths if p.startswith(prefix)]

    def search(self, q: str) -> list[dict[str, Any]]:
        status, data = self._req("GET", f"/search?q={urllib.parse.quote(q)}")
        if status == 404 or not data:
            return []
        res = json.loads(data.decode("utf-8"))
        return res.get("results", res) if isinstance(res, dict) else res


class FakeLapisClient(LapisClient):
    """In-memory vault for tests — dict of path -> content."""
    def __init__(self, files: dict[str, str] | None = None):
        self.files: dict[str, str] = dict(files or {})

    def read_file(self, path):
        return self.files.get(path)

    def write_file(self, path, content):
        self.files[path] = content

    def delete_file(self, path):
        self.files.pop(path, None)

    def list_files(self, prefix=""):
        return [p for p in self.files if p.startswith(prefix)]

    def search(self, q):
        return [{"path": p, "snippet": c[:80]} for p, c in self.files.items() if q.lower() in c.lower()]
