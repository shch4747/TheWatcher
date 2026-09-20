"""Phase 0 exit criterion (docs/Plan - Watcher v1.md):
a message in a test group appears in the buffer; send() posts a reply."""
from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from httpx import ASGITransport
from shared import gateway
from shared.config import settings
from shared.db import Channel, MessageBuffer, OutboundLog, get_session, init_db
from shared.gateway.app import app as gateway_app
from sqlalchemy import select

from tests.fake_gowa.app import app as fake_gowa_app
from tests.gowa_payloads import message_event


async def _allowlist(jid: str, kind: str = "project") -> None:
    async with get_session() as session:
        session.add(Channel(jid=jid, kind=kind))
        await session.commit()


def _sign(body: bytes) -> str:
    digest = hmac.new(settings.gowa_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.fixture(autouse=True)
async def _fresh_db():
    await init_db()
    yield


@pytest.fixture
def gateway_client():
    transport = ASGITransport(app=gateway_app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.fixture(autouse=True)
def _wire_fake_gowa(monkeypatch):
    """Point the gateway's outbound client at the fake gowa app instead of
    a real network call."""
    fake_transport = ASGITransport(app=fake_gowa_app)
    fake_client = httpx.AsyncClient(transport=fake_transport, base_url="http://fake-gowa")
    monkeypatch.setattr(gateway.interface._client, "_client", fake_client)
    monkeypatch.setattr(gateway.interface._client, "base_url", "http://fake-gowa")
    fake_gowa_app.state.sent_messages = []


async def test_webhook_lands_in_buffer(gateway_client):
    await _allowlist("123456789-987654321@g.us")
    payload = message_event("wamid.TESTMSG1", "123456789-987654321@g.us", "hello watcher")
    body = json.dumps(payload).encode()

    async with gateway_client as client:
        resp = await client.post(
            "/webhook/gowa",
            content=body,
            headers={"X-Hub-Signature-256": _sign(body)},
        )
    assert resp.status_code == 200

    async with get_session() as session:
        row = await session.scalar(select(MessageBuffer).where(MessageBuffer.message_id == "wamid.TESTMSG1"))
    assert row is not None
    assert row.channel == "123456789-987654321@g.us"


async def test_webhook_rejects_bad_signature(gateway_client):
    body = json.dumps({"message": {"id": "wamid.BAD"}}).encode()
    async with gateway_client as client:
        resp = await client.post("/webhook/gowa", content=body, headers={"X-Hub-Signature-256": "not-valid"})
    assert resp.status_code == 400

    async with get_session() as session:
        row = await session.scalar(select(MessageBuffer).where(MessageBuffer.message_id == "wamid.BAD"))
    assert row is None


async def test_webhook_dedupes_by_message_id(gateway_client):
    await _allowlist("111-222@g.us")
    payload = message_event("wamid.DUP", "111-222@g.us")
    body = json.dumps(payload).encode()
    headers = {"X-Hub-Signature-256": _sign(body)}
    async with gateway_client as client:
        first = await client.post("/webhook/gowa", content=body, headers=headers)
        second = await client.post("/webhook/gowa", content=body, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 400


async def test_send_posts_to_gowa_and_logs():
    result = await gateway.interface.send("123456789-987654321@g.us", "reply text")
    assert result.message_id is not None
    assert fake_gowa_app.state.sent_messages[-1]["message"] == "reply text"

    async with get_session() as session:
        row = await session.scalar(select(OutboundLog).where(OutboundLog.text == "reply text"))
    assert row is not None
    assert row.message_id == result.message_id
