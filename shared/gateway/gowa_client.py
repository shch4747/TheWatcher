"""Async client over gowa's REST API (aldinokemal/go-whatsapp-web-multidevice).
The Gateway's ONLY outbound path to WhatsApp (ADR-0001) — nothing else in the
app talks to gowa directly.

Endpoints used (verified against
https://github.com/aldinokemal/go-whatsapp-web-multidevice/blob/main/docs/openapi.yaml,
2026-09-20):
  POST /send/message               body: {phone, message, reply_message_id?}
  POST /message/{id}/reaction      body: {phone, emoji}
  GET  /chat/{jid}/messages        ?limit=&offset= -> history backfill
  GET  /app/devices                -> connected device status
  GET  /user/my/groups             -> {results: {data: [{JID, Name, ...}]}},
                                       verified live 2026-09-21 - the only
                                       source of a group's display name;
                                       channel jids alone are meaningless
                                       to a human reading /channels.

Group vs DM addressing is just the `phone` value:
  group : "123456789-987654321@g.us"
  DM    : "628123456789@s.whatsapp.net"
"""
from __future__ import annotations

import asyncio
import base64
import time

import httpx

from shared.config import settings


class RateLimiter:
    """Minimum gap between sends — bursts are what get accounts flagged
    (ADR-0001 consequence: the number is disposable, sends stay conservative)."""

    def __init__(self, min_gap_s: float):
        self.min_gap_s = min_gap_s
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.min_gap_s:
                await asyncio.sleep(self.min_gap_s - delta)
            self._last = time.monotonic()


class GowaClient:
    def __init__(
        self,
        base_url: str | None = None,
        basic_auth: str | None = None,
        device_id: str | None = None,
        min_gap_s: float | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = (base_url or settings.gowa_base_url).rstrip("/")
        self.basic_auth = basic_auth if basic_auth is not None else settings.gowa_basic_auth
        self.device_id = device_id if device_id is not None else settings.gowa_device_id
        self.rate = RateLimiter(min_gap_s if min_gap_s is not None else settings.wa_send_min_gap_s)
        self._client = client or httpx.AsyncClient(timeout=30)

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.basic_auth:
            token = base64.b64encode(self.basic_auth.encode()).decode()
            h["Authorization"] = f"Basic {token}"
        if self.device_id:
            h["X-Device-Id"] = self.device_id
        return h

    async def send_text(self, phone: str, message: str, reply_message_id: str | None = None) -> dict:
        """Send a text to a group JID or DM JID. Rate-limited."""
        await self.rate.wait()
        body: dict = {"phone": phone, "message": message}
        if reply_message_id:
            body["reply_message_id"] = reply_message_id
        resp = await self._client.post(f"{self.base_url}/send/message", json=body, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    async def react(self, message_id: str, phone: str, emoji: str) -> dict:
        resp = await self._client.post(
            f"{self.base_url}/message/{message_id}/reaction",
            json={"phone": phone, "emoji": emoji},
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def get_chat_messages(self, jid: str, limit: int = 100, offset: int = 0) -> list[dict]:
        """Fetch stored history for backfill (Spec: request_history).
        Response is {"results": {"data": [...ChatMessage], "pagination":
        {...}, "chat_info": {...}}} - callers get just the message list."""
        params: dict[str, str | int] = {"limit": min(limit, 100), "offset": offset}
        resp = await self._client.get(
            f"{self.base_url}/chat/{jid}/messages", params=params, headers=self._headers()
        )
        resp.raise_for_status()
        results = resp.json().get("results", {})
        return results.get("data", [])

    async def list_groups(self) -> list[dict]:
        """Every group this session is in, with its display `Name` -
        used to resolve a channel jid to something a human can read
        (Spec: /channels)."""
        resp = await self._client.get(f"{self.base_url}/user/my/groups", headers=self._headers())
        resp.raise_for_status()
        results = resp.json().get("results", {})
        return results.get("data", [])

    async def session_status(self) -> dict:
        try:
            resp = await self._client.get(f"{self.base_url}/app/devices", headers=self._headers())
            resp.raise_for_status()
            return {"ok": True, "data": resp.json()}
        except Exception as e:  # noqa: BLE001 - status probe, never raises
            return {"ok": False, "error": str(e)}

    async def aclose(self) -> None:
        await self._client.aclose()
