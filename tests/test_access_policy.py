"""Tests for vault access policy, channel resolution, and path normalization."""
from __future__ import annotations

import pytest
from shared.db import Channel, get_session
from shared.security.access import (
    AccessPolicy,
    CachedMembership,
    ChannelResolver,
    InvalidPath,
    MembershipUnavailable,
    Principal,
    UnknownChannel,
    filter_paths,
    normalize_path,
)
from sqlalchemy import delete

# Test constants
ALICE_PHONE = "919876543210@s.whatsapp.net"
ALICE_LID = "111222333@lid"
BOB_PHONE = "919123456789@s.whatsapp.net"
CHARLIE_PHONE = "919000000000@s.whatsapp.net"
ADMIN_PHONE = "919999999999@s.whatsapp.net"

COORDIS_JID = "coordis-group@g.us"
EXE_JID = "exe-group@g.us"
PROJECT_JID = "project-alpha@g.us"


class FakeMembership:
    """In-memory membership provider for testing."""

    def __init__(self, roster: dict[str, set[str]] | None = None) -> None:
        self.roster = roster or {}
        self.call_count: dict[str, int] = {}
        self.should_fail: set[str] = set()

    async def members(self, channel_jid: str) -> frozenset[str]:
        self.call_count[channel_jid] = self.call_count.get(channel_jid, 0) + 1
        if channel_jid in self.should_fail:
            raise RuntimeError(f"Connection failed for {channel_jid}")
        return frozenset(self.roster.get(channel_jid, set()))


@pytest.fixture
async def seeded_resolver():
    """Seeds test channels in SQLite and returns a ChannelResolver."""
    test_jids = [COORDIS_JID, EXE_JID, PROJECT_JID]
    async with get_session() as session:
        await session.execute(delete(Channel).where(Channel.jid.in_(test_jids)))
        channels = [
            Channel(jid=COORDIS_JID, kind="coordis", title="Coordis", initiative=None),
            Channel(jid=EXE_JID, kind="exes", title="Executive", initiative=None),
            Channel(jid=PROJECT_JID, kind="project", title="Project Alpha", initiative="alpha"),
        ]
        for ch in channels:
            session.add(ch)
        await session.commit()

    yield ChannelResolver()

    async with get_session() as session:
        await session.execute(delete(Channel).where(Channel.jid.in_(test_jids)))
        await session.commit()


@pytest.fixture
def fake_membership() -> FakeMembership:
    return FakeMembership(
        roster={
            COORDIS_JID: {ALICE_PHONE, ALICE_LID},
            EXE_JID: {BOB_PHONE},
            PROJECT_JID: {ALICE_PHONE, BOB_PHONE},
        }
    )


# --- 1. Member allowed & 2. Non-member denied ---


@pytest.mark.asyncio
async def test_member_allowed(seeded_resolver: ChannelResolver, fake_membership: FakeMembership):
    policy = AccessPolicy(seeded_resolver, fake_membership)
    alice = Principal(identities=frozenset({ALICE_PHONE}), is_admin=False)

    assert await policy.can_read(alice, "channels/project-alpha/thread-001.md") is True


@pytest.mark.asyncio
async def test_non_member_denied(seeded_resolver: ChannelResolver, fake_membership: FakeMembership):
    policy = AccessPolicy(seeded_resolver, fake_membership)
    charlie = Principal(identities=frozenset({CHARLIE_PHONE}), is_admin=False)

    assert await policy.can_read(charlie, "channels/project-alpha/thread-001.md") is False


# --- 3. LID match & 4. Phone match ---


@pytest.mark.asyncio
async def test_lid_match(seeded_resolver: ChannelResolver, fake_membership: FakeMembership):
    policy = AccessPolicy(seeded_resolver, fake_membership)
    alice_lid_only = Principal(identities=frozenset({ALICE_LID}), is_admin=False)

    assert await policy.can_read(alice_lid_only, "channels/project-alpha/active.md") is True


@pytest.mark.asyncio
async def test_phone_match(seeded_resolver: ChannelResolver, fake_membership: FakeMembership):
    policy = AccessPolicy(seeded_resolver, fake_membership)
    bob_phone_only = Principal(identities=frozenset({BOB_PHONE}), is_admin=False)

    assert await policy.can_read(bob_phone_only, "channels/executive/thread-1.md") is True


# --- 5. Archive path & 6. Channel index page ---


@pytest.mark.asyncio
async def test_archive_path(seeded_resolver: ChannelResolver, fake_membership: FakeMembership):
    policy = AccessPolicy(seeded_resolver, fake_membership)
    alice = Principal(identities=frozenset({ALICE_PHONE}), is_admin=False)

    assert await policy.can_read(alice, "channels/archive/project-alpha/old.md") is True


@pytest.mark.asyncio
async def test_channel_index_page(seeded_resolver: ChannelResolver, fake_membership: FakeMembership):
    policy = AccessPolicy(seeded_resolver, fake_membership)
    alice = Principal(identities=frozenset({ALICE_PHONE}), is_admin=False)

    assert await policy.can_read(alice, "channels/project-alpha.md") is True


# --- 7. Unknown slug denied ---


@pytest.mark.asyncio
async def test_unknown_slug_denied(seeded_resolver: ChannelResolver, fake_membership: FakeMembership):
    policy = AccessPolicy(seeded_resolver, fake_membership)
    ordinary = Principal(identities=frozenset({CHARLIE_PHONE}), is_admin=False)

    assert await policy.can_read(ordinary, "channels/unknown-channel/thread.md") is False
    assert await policy.can_read(ordinary, "channels/unknown-channel.md") is False


# --- 8. channels/other.md denied for ordinary member, allowed for admin and privileged ---


@pytest.mark.asyncio
async def test_channels_other_md_access(
    seeded_resolver: ChannelResolver, fake_membership: FakeMembership
):
    policy = AccessPolicy(seeded_resolver, fake_membership)

    ordinary = Principal(identities=frozenset({BOB_PHONE}), is_admin=False, is_privileged=False)
    admin = Principal(identities=frozenset({ADMIN_PHONE}), is_admin=True)
    coordis_member = Principal(identities=frozenset({ALICE_PHONE}), is_admin=False)
    flag_privileged = Principal(identities=frozenset({CHARLIE_PHONE}), is_admin=False, is_privileged=True)

    assert await policy.can_read(ordinary, "channels/other.md") is False
    assert await policy.can_read(admin, "channels/other.md") is True
    assert await policy.can_read(coordis_member, "channels/other.md") is True
    assert await policy.can_read(flag_privileged, "channels/other.md") is True


# --- 9. Membership failure denied ---


@pytest.mark.asyncio
async def test_membership_failure_denied(
    seeded_resolver: ChannelResolver, fake_membership: FakeMembership
):
    fake_membership.should_fail.add(PROJECT_JID)
    cached_membership = CachedMembership(fake_membership)
    policy = AccessPolicy(seeded_resolver, cached_membership)

    bob = Principal(identities=frozenset({BOB_PHONE}), is_admin=False)
    assert await policy.can_read(bob, "channels/project-alpha/thread.md") is False


# --- 10. Admin override ---


@pytest.mark.asyncio
async def test_admin_override(seeded_resolver: ChannelResolver, fake_membership: FakeMembership):
    policy = AccessPolicy(seeded_resolver, fake_membership)
    admin = Principal(identities=frozenset({ADMIN_PHONE}), is_admin=True)

    assert await policy.can_read(admin, "channels/executive/notes.md") is True
    assert await policy.can_read(admin, "inbox/agent.md") is True
    assert await policy.can_read(admin, "channels/other.md") is True
    assert await policy.can_read(admin, "channels/unknown.md") is True


# --- 11. Inbox access ---


@pytest.mark.asyncio
async def test_inbox_denied_unless_admin(
    seeded_resolver: ChannelResolver, fake_membership: FakeMembership
):
    policy = AccessPolicy(seeded_resolver, fake_membership)
    coordis_member = Principal(identities=frozenset({ALICE_PHONE}), is_admin=False)
    flag_privileged = Principal(identities=frozenset({CHARLIE_PHONE}), is_admin=False, is_privileged=True)
    admin = Principal(identities=frozenset({ADMIN_PHONE}), is_admin=True)
    ordinary = Principal(identities=frozenset({BOB_PHONE}), is_admin=False)

    assert await policy.can_read(ordinary, "inbox/wa_agent.md") is False
    assert await policy.can_read(coordis_member, "inbox/wa_agent.md") is True
    assert await policy.can_read(flag_privileged, "inbox/wa_agent.md") is True
    assert await policy.can_read(admin, "inbox/wa_agent.md") is True


# --- 12. Other-area allowed ---


@pytest.mark.asyncio
async def test_other_area_allowed(seeded_resolver: ChannelResolver, fake_membership: FakeMembership):
    policy = AccessPolicy(seeded_resolver, fake_membership)
    ordinary = Principal(identities=frozenset({CHARLIE_PHONE}), is_admin=False)

    assert await policy.can_read(ordinary, "people/alice.md") is True
    assert await policy.can_read(ordinary, "projects/new-bot.md") is True
    assert await policy.can_read(ordinary, "events/hackathon.md") is True
    assert await policy.can_read(ordinary, "resources/links.md") is True
    assert await policy.can_read(ordinary, "ideas/suggestions.md") is True

    # Denied by default on unregistered root areas
    assert await policy.can_read(ordinary, "secrets/passwords.md") is False
    assert await policy.can_read(ordinary, "meta/audit/2026-10.md") is False


# --- 13. Coordis group member reads exe and project pages ---


@pytest.mark.asyncio
async def test_coordis_group_member_reads_exe_and_project(
    seeded_resolver: ChannelResolver, fake_membership: FakeMembership
):
    # Alice is in COORDIS_JID, but NOT in EXE_JID roster
    policy = AccessPolicy(seeded_resolver, fake_membership)
    alice = Principal(identities=frozenset({ALICE_PHONE}), is_admin=False, is_privileged=False)

    assert await policy.can_read(alice, "channels/executive/secret-exec-notes.md") is True
    assert await policy.can_read(alice, "channels/project-alpha/thread.md") is True


# --- 14. Exe not in coordis denied coordis pages ---


@pytest.mark.asyncio
async def test_exe_not_in_coordis_denied_coordis_pages(
    seeded_resolver: ChannelResolver, fake_membership: FakeMembership
):
    # Bob is in EXE_JID, but NOT in COORDIS_JID
    policy = AccessPolicy(seeded_resolver, fake_membership)
    bob = Principal(identities=frozenset({BOB_PHONE}), is_admin=False, is_privileged=False)

    assert await policy.can_read(bob, "channels/coordis/meeting.md") is False
    assert await policy.can_read(bob, "channels/executive/meeting.md") is True


# --- 15. Coordis lookup failure falls back to normal rule ---


@pytest.mark.asyncio
async def test_coordis_lookup_failure_falls_back(
    seeded_resolver: ChannelResolver, fake_membership: FakeMembership
):
    fake_membership.should_fail.add(COORDIS_JID)
    policy = AccessPolicy(seeded_resolver, fake_membership)

    # Bob is in EXE_JID, but coordis lookup fails
    bob = Principal(identities=frozenset({BOB_PHONE}), is_admin=False, is_privileged=False)
    # Normal rule: Bob is in executive, so executive should still succeed
    assert await policy.can_read(bob, "channels/executive/thread.md") is True
    # Bob is not in coordis, coordis lookup fails, normal rule fails: denied
    assert await policy.can_read(bob, "channels/coordis/thread.md") is False


# --- 16. Missing coordis channel never grants via group ---


@pytest.mark.asyncio
async def test_missing_coordis_channel_never_grants_via_group(fake_membership: FakeMembership):
    resolver = ChannelResolver(channels_provider=lambda: _empty_or_no_coordis_channels())
    policy = AccessPolicy(resolver, fake_membership)

    # Alice has identities that would match COORDIS_JID, but no coordis channel is registered
    alice = Principal(identities=frozenset({ALICE_PHONE}), is_admin=False, is_privileged=False)
    assert await policy.can_read(alice, "channels/executive/notes.md") is False


async def _empty_or_no_coordis_channels():
    return [Channel(jid=EXE_JID, kind="exes", title="Executive", initiative=None)]


# --- 17. is_privileged=True reads every channel even when not in coordis & missing/failed coordis ---


@pytest.mark.asyncio
async def test_is_privileged_true_reads_every_channel(fake_membership: FakeMembership):
    fake_membership.should_fail.add(COORDIS_JID)
    resolver = ChannelResolver(channels_provider=lambda: _empty_or_no_coordis_channels())
    policy = AccessPolicy(resolver, fake_membership)

    # Charlie is not in any group, coordis is missing and fails, but has is_privileged=True
    privileged_user = Principal(
        identities=frozenset({CHARLIE_PHONE}), is_admin=False, is_privileged=True
    )

    assert await policy.can_read(privileged_user, "channels/executive/agenda.md") is True
    assert await policy.can_read(privileged_user, "channels/other.md") is True


# --- 18. is_privileged=False and not in coordis group is denied ---


@pytest.mark.asyncio
async def test_not_privileged_and_not_in_coordis_denied(
    seeded_resolver: ChannelResolver, fake_membership: FakeMembership
):
    policy = AccessPolicy(seeded_resolver, fake_membership)
    charlie = Principal(identities=frozenset({CHARLIE_PHONE}), is_admin=False, is_privileged=False)

    assert await policy.can_read(charlie, "channels/executive/agenda.md") is False
    assert await policy.can_read(charlie, "channels/coordis/agenda.md") is False


# --- 19. Adversarial paths never grant access to a channel the asker is not in ---


@pytest.mark.asyncio
async def test_adversarial_paths_never_grant_access(
    seeded_resolver: ChannelResolver, fake_membership: FakeMembership
):
    policy = AccessPolicy(seeded_resolver, fake_membership)
    # Bob is only in EXE_JID, NOT in COORDIS_JID
    bob = Principal(identities=frozenset({BOB_PHONE}), is_admin=False, is_privileged=False)

    adversarial_paths = [
        "channels/coordis/../coordis/meeting.md",
        "../vault/channels/coordis/meeting.md",
        "channels\\coordis\\meeting.md",
        "CHANNELS/COORDIS/MEETING.MD",
        "channels//coordis///meeting.md",
        "/channels/coordis/meeting.md",
        "channels/%2e%2e/coordis/meeting.md",
        "channels/%2E%2E/coordis/meeting.md",
        "channels/..%2fcoordis/meeting.md",
    ]

    for path in adversarial_paths:
        assert await policy.can_read(bob, path) is False, f"Expected denial for: {path}"

    # Verify that valid normalized variations of Bob's own channel work
    assert await policy.can_read(bob, "/channels//executive/meeting.md") is True
    assert await policy.can_read(bob, "channels\\executive\\meeting.md") is True
    assert await policy.can_read(bob, "CHANNELS/EXECUTIVE/MEETING.MD") is True


# --- CachedMembership and helper tests ---


@pytest.mark.asyncio
async def test_cached_membership_caching_and_ttl():
    fake = FakeMembership(roster={"test@g.us": {"user1"}})
    current_time = 1000.0

    def clock():
        return current_time

    cached = CachedMembership(fake, ttl=60.0, clock=clock)

    # First call fetches from inner provider
    members1 = await cached.members("test@g.us")
    assert members1 == frozenset({"user1"})
    assert fake.call_count["test@g.us"] == 1

    # Second call within TTL uses cache
    current_time += 30.0
    members2 = await cached.members("test@g.us")
    assert members2 == frozenset({"user1"})
    assert fake.call_count["test@g.us"] == 1

    # After TTL, calls inner provider again
    current_time += 31.0
    fake.roster["test@g.us"] = {"user1", "user2"}
    members3 = await cached.members("test@g.us")
    assert members3 == frozenset({"user1", "user2"})
    assert fake.call_count["test@g.us"] == 2


@pytest.mark.asyncio
async def test_cached_membership_wraps_errors_as_membership_unavailable():
    fake = FakeMembership()
    fake.should_fail.add("broken@g.us")
    cached = CachedMembership(fake)

    with pytest.raises(MembershipUnavailable):
        await cached.members("broken@g.us")


@pytest.mark.asyncio
async def test_normalize_path_behavior():
    assert normalize_path("/channels//test/file.md") == "channels/test/file.md"
    assert normalize_path("channels\\test\\file.md") == "channels/test/file.md"
    assert normalize_path("CHANNELS/Test/FILE.MD") == "channels/test/file.md"

    with pytest.raises(InvalidPath):
        normalize_path("../secret.md")
    with pytest.raises(InvalidPath):
        normalize_path("channels/../other.md")
    with pytest.raises(InvalidPath):
        normalize_path("channels/%2e%2e/other.md")


@pytest.mark.asyncio
async def test_channel_resolver_outside_and_other():
    resolver = ChannelResolver(channels_provider=lambda: _empty_or_no_coordis_channels())

    # Outside channels returns None
    assert await resolver.resolve("people/alice.md") is None
    assert await resolver.resolve("projects/alpha.md") is None

    # 'other' slug raises UnknownChannel
    with pytest.raises(UnknownChannel):
        await resolver.resolve("channels/other.md")

    # Unrecognised slug raises UnknownChannel
    with pytest.raises(UnknownChannel):
        await resolver.resolve("channels/nonexistent.md")


@pytest.mark.asyncio
async def test_filter_paths_helper(
    seeded_resolver: ChannelResolver, fake_membership: FakeMembership
):
    policy = AccessPolicy(seeded_resolver, fake_membership)
    bob = Principal(identities=frozenset({BOB_PHONE}), is_admin=False)

    paths = [
        "channels/executive/item1.md",
        "channels/coordis/secret.md",
        "people/charlie.md",
        "inbox/agent.md",
    ]

    filtered = await filter_paths(policy, bob, paths)
    assert filtered == ["channels/executive/item1.md", "people/charlie.md"]


# --- Blank and whitespace identity tests ---


@pytest.mark.asyncio
async def test_blank_string_identity_never_grants(
    seeded_resolver: ChannelResolver, fake_membership: FakeMembership
):
    # Both member set and principal have blank strings
    fake_membership.roster[PROJECT_JID].add("")
    policy = AccessPolicy(seeded_resolver, fake_membership)

    blank_user = Principal(identities=frozenset({""}), is_admin=False)
    assert await policy.can_read(blank_user, "channels/project-alpha/thread.md") is False


@pytest.mark.asyncio
async def test_whitespace_only_identity_never_grants(
    seeded_resolver: ChannelResolver, fake_membership: FakeMembership
):
    # Both member set and principal have whitespace strings
    fake_membership.roster[PROJECT_JID].add("   ")
    fake_membership.roster[PROJECT_JID].add("\t\n")
    policy = AccessPolicy(seeded_resolver, fake_membership)

    ws_user = Principal(identities=frozenset({"   ", "\t"}), is_admin=False)
    assert await policy.can_read(ws_user, "channels/project-alpha/thread.md") is False


# --- Ambiguous channels with the same title ---


@pytest.mark.asyncio
async def test_ambiguous_channels_denied_for_ordinary_allowed_for_privileged_and_admin(
    fake_membership: FakeMembership,
):
    # Two distinct channels having the same title "Duplicate"
    dup_channel_1 = Channel(jid="dup1@g.us", kind="project", title="Duplicate", initiative=None)
    dup_channel_2 = Channel(jid="dup2@g.us", kind="project", title="Duplicate", initiative=None)
    fake_membership.roster["dup1@g.us"] = {BOB_PHONE}
    fake_membership.roster["dup2@g.us"] = {BOB_PHONE}

    async def _dup_channels():
        return [dup_channel_1, dup_channel_2]

    resolver = ChannelResolver(channels_provider=_dup_channels)
    policy = AccessPolicy(resolver, fake_membership)

    ordinary = Principal(identities=frozenset({BOB_PHONE}), is_admin=False, is_privileged=False)
    admin = Principal(identities=frozenset({ADMIN_PHONE}), is_admin=True)
    privileged = Principal(identities=frozenset({CHARLIE_PHONE}), is_admin=False, is_privileged=True)

    # Ordinary member is denied due to ambiguity
    assert await policy.can_read(ordinary, "channels/duplicate/thread.md") is False
    assert await policy.can_read(ordinary, "channels/duplicate.md") is False

    # Admin and privileged still read it
    assert await policy.can_read(admin, "channels/duplicate/thread.md") is True
    assert await policy.can_read(privileged, "channels/duplicate/thread.md") is True


# --- Stricter path validation tests ---


@pytest.mark.asyncio
async def test_double_encoded_paths_denied_including_under_people(
    seeded_resolver: ChannelResolver, fake_membership: FakeMembership
):
    policy = AccessPolicy(seeded_resolver, fake_membership)
    ordinary = Principal(identities=frozenset({BOB_PHONE}), is_admin=False)

    assert await policy.can_read(ordinary, "people/%252e%252e") is False
    assert await policy.can_read(ordinary, "people/%252e%252e/alice.md") is False
    assert await policy.can_read(ordinary, "channels/%252e%252e/other.md") is False
    assert await policy.can_read(ordinary, "projects/%2e%2e/test.md") is False


@pytest.mark.asyncio
async def test_paths_with_trailing_dot_space_colon_nul_denied(
    seeded_resolver: ChannelResolver, fake_membership: FakeMembership
):
    policy = AccessPolicy(seeded_resolver, fake_membership)
    ordinary = Principal(identities=frozenset({BOB_PHONE}), is_admin=False)

    # Trailing dot in segment
    assert await policy.can_read(ordinary, "channels/executive./thread.md") is False
    assert await policy.can_read(ordinary, "people/alice./notes.md") is False
    assert await policy.can_read(ordinary, "people/alice.") is False

    # Trailing space in segment
    assert await policy.can_read(ordinary, "channels/executive /thread.md") is False
    assert await policy.can_read(ordinary, "people/alice ") is False

    # Colon in path
    assert await policy.can_read(ordinary, "channels/executive:test.md") is False
    assert await policy.can_read(ordinary, "people/alice:secret.md") is False

    # NUL character in path
    assert await policy.can_read(ordinary, "channels/executive\x00/thread.md") is False
    assert await policy.can_read(ordinary, "people/alice\x00.md") is False


# --- Additional access policy tests ---


@pytest.mark.asyncio
async def test_admin_denied_on_invalid_paths_allowed_on_valid(
    seeded_resolver: ChannelResolver, fake_membership: FakeMembership
):
    policy = AccessPolicy(seeded_resolver, fake_membership)
    admin = Principal(identities=frozenset({ADMIN_PHONE}), is_admin=True)

    assert await policy.can_read(admin, "../x.md") is False
    assert await policy.can_read(admin, "channels/%2e%2e/x.md") is False
    assert await policy.can_read(admin, "channels/executive/notes.md") is True
    assert await policy.can_read(admin, "people/alice.md") is True


@pytest.mark.asyncio
async def test_single_real_channel_titled_other_resolves_and_multiple_deny(
    fake_membership: FakeMembership,
):
    other_channel = Channel(jid="other-jid@g.us", kind="other", title="Other", initiative=None)
    fake_membership.roster["other-jid@g.us"] = {BOB_PHONE}

    async def _single_other():
        return [other_channel]

    resolver = ChannelResolver(channels_provider=_single_other)
    policy = AccessPolicy(resolver, fake_membership)

    member = Principal(identities=frozenset({BOB_PHONE}), is_admin=False)
    non_member = Principal(identities=frozenset({CHARLIE_PHONE}), is_admin=False)

    assert await policy.can_read(member, "channels/other.md") is True
    assert await policy.can_read(non_member, "channels/other.md") is False

    # Two channels titled "Other" make it ambiguous and deny ordinary member
    other_dup = Channel(jid="other-dup@g.us", kind="other", title="Other", initiative=None)
    fake_membership.roster["other-dup@g.us"] = {BOB_PHONE}

    async def _dup_other():
        return [other_channel, other_dup]

    dup_resolver = ChannelResolver(channels_provider=_dup_other)
    dup_policy = AccessPolicy(dup_resolver, fake_membership)

    assert await dup_policy.can_read(member, "channels/other.md") is False


@pytest.mark.asyncio
async def test_blank_string_in_coordis_never_grants_privilege(
    seeded_resolver: ChannelResolver, fake_membership: FakeMembership
):
    fake_membership.roster[COORDIS_JID].add("")
    fake_membership.roster[COORDIS_JID].add("   ")
    policy = AccessPolicy(seeded_resolver, fake_membership)

    blank_user = Principal(identities=frozenset({"", "  "}), is_admin=False)
    assert await policy.can_read(blank_user, "channels/executive/notes.md") is False
    assert await policy.can_read(blank_user, "inbox/agent.md") is False
