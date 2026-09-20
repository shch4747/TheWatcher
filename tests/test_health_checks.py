"""Health-check functions for gowa and the vault, separate from each
other and from the static /healthz - needed to tell "watcher is up" from
"watcher is up but can't reach gowa/Lapis"."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport
from shared.gateway import interface as gateway
from shared.wiki.interface import LocalDirClient, check_vault_connection

from tests.fake_gowa.app import app as fake_gowa_app


@pytest.fixture(autouse=True)
def _wire_fake_gowa(monkeypatch):
    fake_transport = ASGITransport(app=fake_gowa_app)
    fake_client = httpx.AsyncClient(transport=fake_transport, base_url="http://fake-gowa")
    monkeypatch.setattr(gateway._client, "_client", fake_client)
    monkeypatch.setattr(gateway._client, "base_url", "http://fake-gowa")


async def test_check_gowa_connection_ok():
    result = await gateway.check_gowa_connection()
    assert result["ok"] is True


async def test_check_gowa_connection_reports_failure(monkeypatch: pytest.MonkeyPatch):
    async def _broken_get(*args, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(gateway._client._client, "get", _broken_get)
    result = await gateway.check_gowa_connection()
    assert result["ok"] is False
    assert "error" in result


async def test_check_vault_connection_local_dir_ok(tmp_path: Path):
    vault = LocalDirClient(tmp_path)
    result = await check_vault_connection(vault)
    assert result["ok"] is True
    assert result["path_count"] == 0


async def test_check_vault_connection_reports_failure():
    class BrokenVault:
        async def list(self, prefix: str) -> list[str]:
            raise RuntimeError("vault unreachable")

    result = await check_vault_connection(BrokenVault())
    assert result["ok"] is False
    assert "vault unreachable" in result["error"]
