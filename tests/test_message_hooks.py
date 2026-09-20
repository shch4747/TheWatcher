"""Real-time message hooks (e.g. the Chat Agent) fire for regular
messages in an allowlisted channel, never for admin commands, reactions,
duplicates, or edits - and a broken hook never breaks webhook intake."""
from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from httpx import ASGITransport
from shared.config import settings
from shared.db import BotAdmin, Channel, get_session
from shared.gateway import interface as gateway
from shared.gateway.app import app as gateway_app

from tests.fake_gowa.app import app as fake_gowa_app


def _sign(body: bytes) -> str:
    return hmac.new(settings.gowa_webhook_secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def _wire_fake_gowa(monkeypatch):
    fake_transport = ASGITransport(app=fake_gowa_app)
    fake_client = httpx.AsyncClient(transport=fake_transport, base_url="http://fake-gowa")
    monkeypatch.setattr(gateway._client, "_client", fake_client)
    monkeypatch.setattr(gateway._client, "base_url", "http://fake-gowa")
    fake_gowa_app.state.sent_messages = []


@pytest.fixture(autouse=True)
def _clear_hooks():
    gateway._message_hooks.clear()
    yield
    gateway._message_hooks.clear()


@pytest.fixture
def client():
    return httpx.AsyncClient(transport=ASGITransport(app=gateway_app), base_url="http://testserver")


async def _allowlist(jid: str) -> None:
    async with get_session() as session:
        session.add(Channel(jid=jid, kind="project"))
        await session.commit()


async def test_hook_fires_for_regular_message(client: httpx.AsyncClient):
    seen = []

    async def hook(payload: dict, channel: str) -> None:
        seen.append((payload["message"]["text"], channel))

    gateway.register_message_hook(hook)
    await _allowlist("hook-chan-1@g.us")

    body = json.dumps(
        {"event": "message", "from": "hook-chan-1@g.us", "message": {"id": "hk1", "text": "hello"}}
    ).encode()
    async with client as c:
        await c.post("/webhook/gowa", content=body, headers={"X-Hub-Signature-256": _sign(body)})

    assert seen == [("hello", "hook-chan-1@g.us")]


async def test_hook_does_not_fire_for_admin_command(client: httpx.AsyncClient):
    seen = []

    async def hook(payload: dict, channel: str) -> None:
        seen.append(payload)

    gateway.register_message_hook(hook)
    async with get_session() as session:
        session.add(BotAdmin(wa_identity="admin@hook"))
        await session.commit()

    body = json.dumps(
        {
            "event": "message",
            "from": "hook-chan-cmd@g.us",
            "sender": "admin@hook",
            "message": {"id": "hk2", "text": "/status"},
        }
    ).encode()
    async with client as c:
        await c.post("/webhook/gowa", content=body, headers={"X-Hub-Signature-256": _sign(body)})

    assert seen == []


async def test_hook_does_not_fire_for_duplicate_message(client: httpx.AsyncClient):
    seen = []

    async def hook(payload: dict, channel: str) -> None:
        seen.append(payload)

    gateway.register_message_hook(hook)
    await _allowlist("hook-chan-dup@g.us")

    body = json.dumps(
        {"event": "message", "from": "hook-chan-dup@g.us", "message": {"id": "hk3", "text": "hi"}}
    ).encode()
    headers = {"X-Hub-Signature-256": _sign(body)}
    async with client as c:
        await c.post("/webhook/gowa", content=body, headers=headers)
        await c.post("/webhook/gowa", content=body, headers=headers)

    assert len(seen) == 1


async def test_broken_hook_does_not_break_webhook_intake(client: httpx.AsyncClient):
    async def broken_hook(payload: dict, channel: str) -> None:
        raise RuntimeError("boom")

    gateway.register_message_hook(broken_hook)
    await _allowlist("hook-chan-broken@g.us")

    body = json.dumps(
        {"event": "message", "from": "hook-chan-broken@g.us", "message": {"id": "hk4", "text": "hi"}}
    ).encode()
    async with client as c:
        resp = await c.post("/webhook/gowa", content=body, headers={"X-Hub-Signature-256": _sign(body)})

    assert resp.status_code == 200
