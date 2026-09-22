"""Phase 6 HTTP adapter (Spec, Shape: "thin HTTP adapter exposes the
same interfaces as JSON endpoints... so the contract is exercised").
Every route is a pass-through - these tests exercise the contract, not
new logic."""
from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport
from shared.db import Channel, MessageBuffer, get_session
from shared.gateway import interface as gateway
from shared.gateway.app import app as gateway_app

from tests.fake_gowa.app import app as fake_gowa_app


@pytest.fixture(autouse=True)
def _wire_fake_gowa(monkeypatch):
    fake_transport = ASGITransport(app=fake_gowa_app)
    fake_client = httpx.AsyncClient(transport=fake_transport, base_url="http://fake-gowa")
    monkeypatch.setattr(gateway._client, "_client", fake_client)
    monkeypatch.setattr(gateway._client, "base_url", "http://fake-gowa")
    fake_gowa_app.state.sent_messages = []


@pytest.fixture
def client():
    return httpx.AsyncClient(transport=ASGITransport(app=gateway_app), base_url="http://testserver")


async def test_send_endpoint(client: httpx.AsyncClient):
    async with client as c:
        resp = await c.post("/api/gateway/send", json={"channel": "http-chan@g.us", "text": "hi"})
    assert resp.status_code == 200
    assert resp.json()["message_id"] is not None


async def test_get_message_endpoint_404_and_hit(client: httpx.AsyncClient):
    async with get_session() as session:
        session.add(
            MessageBuffer(
                message_id="http-m1", channel="c@g.us", event_type="message", payload='{"id": "http-m1"}'
            )
        )
        await session.commit()

    async with client as c:
        missing = await c.get("/api/gateway/messages/does-not-exist")
        hit = await c.get("/api/gateway/messages/http-m1")

    assert missing.status_code == 404
    assert hit.status_code == 200
    assert hit.json()["id"] == "http-m1"


async def test_channels_endpoints(client: httpx.AsyncClient):
    async with get_session() as session:
        session.add(Channel(jid="http-list-chan@g.us", kind="project", title="HttpProj"))
        await session.commit()

    async with client as c:
        listed = await c.get("/api/gateway/channels", params={"kind": "project"})
        single = await c.get("/api/gateway/channels/http-list-chan@g.us")
        missing = await c.get("/api/gateway/channels/nope@g.us")

    assert any(row["jid"] == "http-list-chan@g.us" for row in listed.json())
    assert single.json()["title"] == "HttpProj"
    assert missing.status_code == 404


async def test_lint_endpoint(client: httpx.AsyncClient):
    """A page missing its managed/append/derived skeletons is reported
    (additively repairable). Phone/email-shaped strings are NOT reported
    any more - ADR-0012 amends ADR-0009 for this internal wiki."""
    text = (
        "---\ntype: project\nslug: a\ntitle: A\nstatus: active\n"
        'lead: "[[X]]"\n---\n# A\n## Brief\ncontact me at a@b.com\n'
    )
    async with client as c:
        resp = await c.post("/api/wiki/lint", json={"path": "a.md", "text": text})
    issues = resp.json()
    assert any("missing managed section" in i["message"] for i in issues)
    assert not any("email" in i["message"] for i in issues)


async def test_due_jobs_endpoint(client: httpx.AsyncClient):
    async with client as c:
        resp = await c.get("/api/scheduler/due-jobs")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_webhook_secret_rotation_accepts_both_old_and_new(monkeypatch: pytest.MonkeyPatch):
    import hashlib
    import hmac
    import json

    from shared.config import settings

    monkeypatch.setattr(settings, "gowa_webhook_secret", "new-secret")
    monkeypatch.setattr(settings, "gowa_webhook_secret_previous", "old-secret")

    async with get_session() as session:
        session.add(Channel(jid="rotation-chan@g.us", kind="project"))
        await session.commit()

    payload = json.dumps(
        {"event": "message", "from": "rotation-chan@g.us", "message": {"id": "rot-m1", "text": "hi"}}
    ).encode()
    old_sig = hmac.new(b"old-secret", payload, hashlib.sha256).hexdigest()
    new_sig = hmac.new(b"new-secret", payload, hashlib.sha256).hexdigest()
    bad_sig = hmac.new(b"wrong-secret", payload, hashlib.sha256).hexdigest()

    assert gateway.verify_signature(payload, old_sig) is True
    assert gateway.verify_signature(payload, new_sig) is True
    assert gateway.verify_signature(payload, bad_sig) is False
