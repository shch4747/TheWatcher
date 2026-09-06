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

    def manifest_hash(self) -> str:
        """Return a hash of the vault's current state. Used for cache
        invalidation — only re-read when this changes."""
        return ""

    def is_reachable(self) -> bool:
        """Quick connectivity check. Returns True if the server responds."""
        return True


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
        # -- manifest-hash caching --
        self._manifest_hash: str = ""
        self._manifest_cache: list[str] = []     # cached file list from manifest
        self._file_cache: dict[str, str] = {}    # path -> content cache
        self._cache_dirty = True                  # force first read

    def _url(self, suffix: str) -> str:
        return f"{self.base_url}/api/vaults/{self.vault_id}{suffix}"

    def _req(self, method: str, suffix: str, body: bytes | None = None,
             content_type: str = "text/markdown") -> tuple[int, bytes]:
        headers = {"User-Agent": "ARIES-TheWatcher/1.0"}
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

    # -- caching layer -------------------------------------------------------

    def _refresh_manifest(self) -> list[str]:
        """Fetch the manifest and update the file-list cache."""
        status, data = self._req("GET", "/manifest")
        if status == 404 or not data:
            self._manifest_cache = []
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
        self._manifest_cache = paths
        # Extract hash if returned (varies by Lapis build)
        if isinstance(manifest, dict):
            new_hash = str(manifest.get("hash") or manifest.get("etag") or manifest.get("version") or "")
            if new_hash and new_hash != self._manifest_hash:
                # Vault changed — invalidate file cache
                self._file_cache.clear()
                self._manifest_hash = new_hash
                self._cache_dirty = False
            elif not new_hash:
                # No hash available — always invalidate (safe default)
                self._file_cache.clear()
                self._cache_dirty = False
        return paths

    def manifest_hash(self) -> str:
        """Return the vault's current manifest hash. One cheap call to check
        whether anything changed since last read."""
        self._refresh_manifest()
        return self._manifest_hash

    def invalidate_cache(self, path: str | None = None) -> None:
        """Invalidate cached reads. Pass a path to clear one entry, or None
        to clear all. Called automatically on write/delete."""
        if path:
            self._file_cache.pop(path, None)
        else:
            self._file_cache.clear()
        self._cache_dirty = True

    # -- core operations (with caching) ------------------------------------

    def read_file(self, path: str) -> Optional[str]:
        # Check cache first
        if path in self._file_cache:
            return self._file_cache[path]
        status, data = self._req("GET", f"/files/{self._encode_path(path)}")
        if status == 404:
            return None
        content = data.decode("utf-8")
        self._file_cache[path] = content
        return content

    def write_file(self, path: str, content: str) -> None:
        self._req("PUT", f"/files/{self._encode_path(path)}", body=content.encode("utf-8"))
        # Update cache with what we just wrote (avoid stale reads)
        self._file_cache[path] = content
        self._cache_dirty = True

    def delete_file(self, path: str) -> None:
        self._req("DELETE", f"/files/{self._encode_path(path)}")
        self._file_cache.pop(path, None)
        self._cache_dirty = True

    def list_files(self, prefix: str = "") -> list[str]:
        # Re-fetch manifest if dirty (a write happened since last list)
        if self._cache_dirty:
            self._refresh_manifest()
        return [p for p in self._manifest_cache if p.startswith(prefix)]

    def search(self, q: str) -> list[dict[str, Any]]:
        status, data = self._req("GET", f"/search?q={urllib.parse.quote(q)}")
        if status == 404 or not data:
            return []
        res = json.loads(data.decode("utf-8"))
        return res.get("results", res) if isinstance(res, dict) else res

    def is_reachable(self) -> bool:
        """Quick check: can we reach the Lapis server?"""
        try:
            self._req("GET", "/manifest")
            return True
        except Exception:
            return False


class FakeLapisClient(LapisClient):
    """In-memory vault for tests — dict of path -> content."""
    def __init__(self, files: dict[str, str] | None = None):
        self.files: dict[str, str] = dict(files or {})
        self._hash_counter = 0

    def read_file(self, path):
        return self.files.get(path)

    def write_file(self, path, content):
        self.files[path] = content
        self._hash_counter += 1

    def delete_file(self, path):
        self.files.pop(path, None)
        self._hash_counter += 1

    def list_files(self, prefix=""):
        return [p for p in self.files if p.startswith(prefix)]

    def search(self, q):
        return [{"path": p, "snippet": c[:80]} for p, c in self.files.items() if q.lower() in c.lower()]

    def manifest_hash(self):
        return str(self._hash_counter)

    def is_reachable(self):
        return True
