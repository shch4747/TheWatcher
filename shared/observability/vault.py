"""Audit wrapper around the wiki client (ADR-0013): every change the
agents make to Lapis gets a row in `obs_vault_ops`.

Wrapping the four-method `VaultClient` protocol rather than editing call
sites means this catches *every* mutation in the repo - the Gateway's
`/setup` and proposal writes, the WA Agent's thread pages and archival
deletes, the Project Agent's initiative writes - with no change to any
of them, and it works for both `LapisClient` and `LocalDirClient`.

`read`/`list` are pure reads and are not recorded; logging them would
multiply the table by an order of magnitude for no audit value.
"""
from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime

from shared.observability.record import CONFLICT, ERROR, OK, record_vault_op
from shared.wiki.lapis_client import ConflictError, ReadResult, VaultClient, WriteResult


class ObservedVaultClient:
    """Delegates everything; records `write` and `delete`. Exceptions are
    recorded and then re-raised unchanged - an audit trail that alters
    behaviour is worse than none."""

    def __init__(self, inner: VaultClient):
        self._inner = inner

    @property
    def inner(self) -> VaultClient:
        """The wrapped client, for code that needs to know what it really
        is (e.g. `/health` reporting Lapis vs local directory)."""
        return self._inner

    async def read(self, path: str) -> ReadResult:
        return await self._inner.read(path)

    async def list(self, prefix: str) -> list[str]:
        return await self._inner.list(prefix)

    async def write(self, path: str, content: str, base_revision: str) -> WriteResult:
        started = time.monotonic()
        at = datetime.now(UTC)
        encoded = content.encode()
        content_bytes = len(encoded)
        content_sha256 = hashlib.sha256(encoded).hexdigest()

        async def _record(outcome: str, new_revision: str | None, error: str | None) -> None:
            await record_vault_op(
                path,
                "write",
                outcome,
                base_revision=base_revision or None,
                new_revision=new_revision,
                content_bytes=content_bytes,
                content_sha256=content_sha256,
                duration_ms=_ms(started),
                error=error,
                at=at,
            )

        try:
            result = await self._inner.write(path, content, base_revision)
        except ConflictError as exc:
            await _record(CONFLICT, getattr(exc, "current_revision", None), str(exc))
            raise
        except Exception as exc:
            await _record(ERROR, None, f"{type(exc).__name__}: {exc}")
            raise
        await _record(OK, result.revision, None)
        return result

    async def delete(self, path: str) -> None:
        started = time.monotonic()
        at = datetime.now(UTC)
        try:
            await self._inner.delete(path)
        except Exception as exc:
            await record_vault_op(
                path, "delete", ERROR, duration_ms=_ms(started),
                error=f"{type(exc).__name__}: {exc}", at=at,
            )
            raise
        await record_vault_op(path, "delete", OK, duration_ms=_ms(started), at=at)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
