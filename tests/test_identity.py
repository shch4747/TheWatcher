"""Phase 2 identity/CMS acceptance criteria (docs/Spec - Watcher v1.md,
"Gateway": identity resolution): unknown senders are fuzzy-matched and
proposed to Bot Admins; confirm links the registry keyed on CMS id;
no match offers to create a member page instead of guessing."""
from __future__ import annotations

from datetime import UTC
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport
from shared.cms.interface import SeedFileCmsClient, fuzzy_match_member
from shared.db import Channel, MembersRegistry, Proposal, get_session
from shared.gateway import interface as gateway
from shared.wiki.interface import LocalDirClient
from sqlalchemy import select

from tests.fake_gowa.app import app as fake_gowa_app

FIXTURES = Path(__file__).parent / "fixtures" / "cms" / "members_seed.json"


@pytest.fixture
def cms_client() -> SeedFileCmsClient:
    return SeedFileCmsClient(FIXTURES)


@pytest.fixture(autouse=True)
def _wire_fake_gowa(monkeypatch):
    fake_transport = ASGITransport(app=fake_gowa_app)
    fake_client = httpx.AsyncClient(transport=fake_transport, base_url="http://fake-gowa")
    monkeypatch.setattr(gateway._client, "_client", fake_client)
    monkeypatch.setattr(gateway._client, "base_url", "http://fake-gowa")
    fake_gowa_app.state.sent_messages = []


@pytest.fixture
def vault(tmp_path: Path) -> LocalDirClient:
    return LocalDirClient(tmp_path)


async def _seed_coordis_channel(jid: str = "coordis-group@g.us") -> str:
    async with get_session() as session:
        session.add(Channel(jid=jid, kind="coordis"))
        await session.commit()
    return jid


async def test_seed_file_cms_client_reads_fixture(cms_client: SeedFileCmsClient):
    members = await cms_client.list_members()
    assert {m.title for m in members} == {"Aira", "Devansh", "Zara Khan"}
    assert (await cms_client.get_member("cms-1")).title == "Aira"
    assert await cms_client.get_member("nope") is None


def test_fuzzy_match_finds_close_name():
    from shared.cms.interface import MemberRecord

    candidates = [MemberRecord(cms_id="1", title="Aira"), MemberRecord(cms_id="2", title="Devansh")]
    match = fuzzy_match_member("Aira J", candidates)
    assert match is not None
    assert match.title == "Aira"


def test_fuzzy_match_returns_none_below_threshold():
    from shared.cms.interface import MemberRecord

    candidates = [MemberRecord(cms_id="1", title="Aira"), MemberRecord(cms_id="2", title="Devansh")]
    assert fuzzy_match_member("Completely Unrelated Name", candidates) is None


async def test_unknown_sender_is_proposed_and_confirm_links_registry(
    cms_client: SeedFileCmsClient, vault: LocalDirClient
):
    await _seed_coordis_channel()

    text = await gateway.propose_identity_link("919876500001@s.whatsapp.net", "Aira J", cms_client)
    assert "Aira" in text
    assert "\U0001f44d" in text

    async with get_session() as session:
        proposal = await session.scalar(select(Proposal).where(Proposal.kind == "link_member"))
    assert proposal is not None
    assert proposal.status == "pending"
    assert proposal.message_id is not None  # sent to the coordis channel

    reply = await gateway.confirm_proposal(proposal.id, confirmed_by="admin@example", vault=vault)
    assert "Linked" in reply

    async with get_session() as session:
        row = await session.get(MembersRegistry, "919876500001@s.whatsapp.net")
        refreshed = await session.get(Proposal, proposal.id)
    assert row is not None
    assert row.member_title == "Aira"
    assert row.cms_member_id == "cms-1"
    assert refreshed.status == "confirmed"


async def test_no_match_offers_to_create_member_page(cms_client: SeedFileCmsClient, vault: LocalDirClient):
    await _seed_coordis_channel("coordis-group-2@g.us")

    wa_identity = "918888888888@s.whatsapp.net"
    text = await gateway.propose_identity_link(wa_identity, "Totally New Person", cms_client)
    assert "create a member page" in text.lower()

    async with get_session() as session:
        proposal = await session.scalar(select(Proposal).where(Proposal.kind == "create_member"))
    assert proposal is not None

    reply = await gateway.confirm_proposal(proposal.id, confirmed_by="admin@example", vault=vault)
    assert "Created" in reply

    page = (await vault.read("people/totally-new-person.md")).content
    assert "type: member" in page

    async with get_session() as session:
        row = await session.get(MembersRegistry, "918888888888@s.whatsapp.net")
    assert row is not None
    assert row.member_title == "Totally New Person"


async def test_already_linked_sender_short_circuits(cms_client: SeedFileCmsClient):
    async with get_session() as session:
        session.add(MembersRegistry(wa_identity="already@linked", member_title="Devansh"))
        await session.commit()

    text = await gateway.propose_identity_link("already@linked", "Devansh", cms_client)
    assert "already linked" in text.lower()

    async with get_session() as session:
        count = len(
            list(await session.scalars(select(Proposal).where(Proposal.channel == "unknown")))
        )
    # no new proposal was created for an already-linked sender
    assert count == 0


async def test_expired_proposal_cannot_be_confirmed(cms_client: SeedFileCmsClient, vault: LocalDirClient):
    from datetime import datetime, timedelta

    async with get_session() as session:
        proposal = Proposal(
            channel="x",
            kind="link_member",
            payload='{"wa_identity": "x", "member_title": "Aira", "cms_member_id": "cms-1"}',
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        session.add(proposal)
        await session.commit()
        proposal_id = proposal.id

    reply = await gateway.confirm_proposal(proposal_id, confirmed_by="admin@example", vault=vault)
    assert "expired" in reply.lower()

    async with get_session() as session:
        row = await session.get(Proposal, proposal_id)
    assert row.status == "expired"
