"""Vault access policy and channel resolution.

Governs read permissions across the wiki vault based on caller identities,
channel membership, and administrative/privileged roles.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db import Channel, get_session
from shared.wiki.layout import ChannelLike, channel_dir
from shared.wiki.templates import slugify


class SecurityError(Exception):
    """Base class for security and access policy exceptions."""


class MembershipUnavailable(SecurityError):
    """Raised when channel membership lookup fails."""


class UnknownChannel(SecurityError):
    """Raised when a channel slug is not recognised or lacks a WhatsApp JID."""


class InvalidPath(SecurityError):
    """Raised when a path is invalid or attempts traversal."""


@dataclass(frozen=True)
class Principal:
    """Represents the caller requesting access."""

    identities: frozenset[str]
    is_admin: bool
    is_privileged: bool = False


class MembershipProvider(Protocol):
    """Interface for channel membership lookup."""

    async def members(self, channel_jid: str) -> frozenset[str]: ...


class CachedMembership:
    """Caches channel membership sets with a TTL (default 60 seconds).

    Any inner error from the underlying provider is caught and re-raised as
    MembershipUnavailable.
    """

    def __init__(
        self,
        provider: MembershipProvider,
        ttl: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self.ttl = ttl
        self.clock = clock
        self._cache: dict[str, tuple[float, frozenset[str]]] = {}

    async def members(self, channel_jid: str) -> frozenset[str]:
        now = self.clock()
        if channel_jid in self._cache:
            expires_at, cached_members = self._cache[channel_jid]
            if now < expires_at:
                return cached_members

        try:
            res = await self.provider.members(channel_jid)
        except Exception as exc:
            raise MembershipUnavailable(f"Failed to fetch members for {channel_jid}: {exc}") from exc

        member_set = frozenset(m.strip() for m in res if isinstance(m, str) and m.strip())
        self._cache[channel_jid] = (now + self.ttl, member_set)
        return member_set


def normalize_path(path: str) -> str:
    """Normalize a vault path for access evaluation.

    Strips leading slashes, converts backslashes to forward slashes,
    collapses duplicate slashes, checks for and rejects '..' traversal
    segments with InvalidPath, and lowercases the result for matching only.

    Raises InvalidPath for:
      - Any path containing '%', ':', NUL, or other control characters.
      - Any path segment ending in '.' or a space.
      - Any '..' traversal segment.
    """
    if not isinstance(path, str):
        raise InvalidPath("Path must be a string")

    # Reject forbidden characters: '%', ':', NUL, or ASCII control characters
    if "%" in path or ":" in path:
        raise InvalidPath("Path contains forbidden character ('%' or ':')")

    for ch in path:
        code = ord(ch)
        if code < 32 or code == 127:
            raise InvalidPath("Path contains NUL or control characters")

    p = path.replace("\\", "/")

    # Check segments for traversal, trailing dot, or trailing space
    parts = [seg for seg in p.split("/") if seg]
    for seg in parts:
        if seg == "..":
            raise InvalidPath(f"Path contains invalid '..' segment: {path}")
        if seg.endswith(".") or seg.endswith(" "):
            raise InvalidPath(f"Path segment ends with dot or space: {seg}")

    clean_parts = [seg for seg in parts if seg != "."]
    if not clean_parts:
        return ""

    return "/".join(clean_parts).lower()


class ChannelResolver:
    """Resolves vault channel paths to WhatsApp channel JIDs.

    Maps:
      - channels/<slug>/...
      - channels/<slug>.md
      - channels/archive/<slug>/...

    Returns None outside channels/.
    Raises UnknownChannel for an unrecognised slug under channels/, for an
    ambiguous slug matching multiple channels, or for a slug with no WhatsApp JID.
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession] | None = None,
        channels_provider: Callable[[], Awaitable[list[ChannelLike]]] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._channels_provider = channels_provider

    async def get_channels(self) -> list[ChannelLike]:
        if self._channels_provider is not None:
            return await self._channels_provider()
        session_factory = self._session_factory or get_session
        async with session_factory() as session:
            rows = await session.scalars(select(Channel))
            return list(rows)

    async def coordis_jid(self) -> str | None:
        """Return the WhatsApp JID of the coordis channel, or None if not set up."""
        try:
            channels = await self.get_channels()
            for ch in channels:
                if getattr(ch, "kind", None) == "coordis":
                    jid = getattr(ch, "jid", "")
                    if jid and "@" in jid:
                        return jid
        except Exception:
            return None
        return None

    async def resolve(self, path: str) -> str | None:
        """Resolve a vault path to a channel WhatsApp JID.

        Returns None if the path is outside channels/.
        Raises UnknownChannel if the slug is unknown, ambiguous, or lacks a WhatsApp JID.
        """
        norm = normalize_path(path)
        parts = norm.split("/")
        if not parts or parts[0] != "channels":
            return None

        # Extract slug
        if len(parts) >= 3 and parts[1] == "archive":
            slug_part = parts[2]
        elif len(parts) >= 2:
            slug_part = parts[1]
        else:
            raise UnknownChannel(f"Path does not specify a channel slug: {path}")

        slug = slug_part[:-3] if slug_part.endswith(".md") else slug_part
        if not slug:
            raise UnknownChannel(f"Path does not specify a channel slug: {path}")

        channels = await self.get_channels()
        matching_jids: set[str] = set()
        matched_without_jid = False

        for ch in channels:
            ch_slug = channel_dir(ch)
            jid = getattr(ch, "jid", "")
            if ch_slug == slug or (jid and slugify(jid) == slug):
                if jid and "@" in jid:
                    matching_jids.add(jid)
                else:
                    matched_without_jid = True

        if len(matching_jids) > 1:
            raise UnknownChannel(f"Channel slug '{slug}' is ambiguous (matches multiple channels)")
        if len(matching_jids) == 1:
            return next(iter(matching_jids))
        if matched_without_jid:
            raise UnknownChannel(f"Channel slug '{slug}' has no WhatsApp JID")

        raise UnknownChannel(f"Unrecognised channel slug: '{slug}'")


class AccessPolicy:
    """Access policy governing read permissions across the vault.

    Denies by default, evaluated in the following order:
    1. Invalid paths are denied for everyone (including admins).
    2. is_admin allowed on all valid paths (including inbox/).
    3. Privileged readers allowed everywhere (including inbox/). A reader is
       privileged when principal.is_privileged is True OR any identity is in
       the coordis channel members. If the coordis channel is not set up or its
       lookup fails, ONLY the is_privileged flag can grant (the group never does).
    4. inbox/ denied unless admin or privileged.
    5. Channel paths (channels/<slug>/..., channels/<slug>.md,
       channels/archive/<slug>/...) are allowed only if an identity is in that
       channel's members. MembershipUnavailable, UnknownChannel, and InvalidPath
       all deny. Empty or whitespace-only identities are discarded, and access
       is denied if the principal has no non-blank identity.
    6. Other areas (people/, projects/, events/, resources/, ideas/) are allowed.
       This is a deliberate choice for now: organizational knowledge and shared
       context across these core wiki sections remain open to all members,
       while conversational channels, private threads, and agent inboxes are
       protected by channel-level access boundaries.

    All other paths are denied by default.
    """

    OTHER_ALLOWED_AREAS = frozenset({"people", "projects", "events", "resources", "ideas"})

    def __init__(self, resolver: ChannelResolver, membership: MembershipProvider) -> None:
        self.resolver = resolver
        self.membership = membership

    async def can_read(self, principal: Principal, path: str) -> bool:
        # Normalize path first; InvalidPath denies everyone including admin
        try:
            norm = normalize_path(path)
        except InvalidPath:
            return False

        if not norm:
            return False

        # (1) is_admin allowed on all valid paths
        if principal.is_admin:
            return True

        # Drop empty or whitespace-only identities
        clean_identities = frozenset(
            i.strip() for i in principal.identities if isinstance(i, str) and i.strip()
        )

        # (2) Privileged readers allowed everywhere (including inbox/)
        is_privileged_reader = False
        if principal.is_privileged:
            is_privileged_reader = True
        else:
            try:
                coordis_jid = await self.resolver.coordis_jid()
                if coordis_jid:
                    coordis_members = await self.membership.members(coordis_jid)
                    clean_coordis = frozenset(
                        m.strip() for m in coordis_members if isinstance(m, str) and m.strip()
                    )
                    if clean_identities and bool(clean_identities & clean_coordis):
                        is_privileged_reader = True
            except Exception:
                # If coordis channel is not set up or lookup fails, group never grants
                pass

        if is_privileged_reader:
            return True

        # (3) inbox/ denied unless admin or privileged
        if norm.startswith("inbox/") or norm == "inbox":
            return False

        # (4) Channel paths allowed only for members with non-blank identities
        parts = norm.split("/")
        if parts[0] == "channels":
            if not clean_identities:
                return False
            try:
                channel_jid = await self.resolver.resolve(norm)
                if not channel_jid:
                    return False
                members = await self.membership.members(channel_jid)
                clean_members = frozenset(
                    m.strip() for m in members if isinstance(m, str) and m.strip()
                )
                return bool(clean_identities & clean_members)
            except (MembershipUnavailable, UnknownChannel, InvalidPath):
                return False
            except Exception:
                return False

        # (5) Other areas allowed (deliberate choice for now)
        if parts[0] in self.OTHER_ALLOWED_AREAS:
            return True

        # Deny by default
        return False

    async def filter_paths(self, principal: Principal, paths: Iterable[str]) -> list[str]:
        """Filter a collection of paths to only those readable by the principal."""
        allowed: list[str] = []
        for path in paths:
            if await self.can_read(principal, path):
                allowed.append(path)
        return allowed


async def filter_paths(
    policy: AccessPolicy, principal: Principal, paths: Iterable[str]
) -> list[str]:
    """Helper to filter search results according to an AccessPolicy."""
    return await policy.filter_paths(principal, paths)
