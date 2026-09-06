"""
Thin client over gowa's REST API (aldinokemal/go-whatsapp-web-multidevice,
v8+). The WA agent's ONLY outbound path to WhatsApp — all sends go through
here so rate-limiting and formatting live in one place (ban-avoidance).

Config via env:
  GOWA_BASE_URL      e.g. http://localhost:3000
  GOWA_BASIC_AUTH    "user:pass" (gowa's default basic auth), optional
  GOWA_DEVICE_ID     device JID, sent as X-Device-Id (multi-account setups)
  WA_SEND_MIN_GAP_S  min seconds between sends (default 4) — humanize output

Endpoints used (from gowa's OpenAPI):
  POST /send/message   body: {phone, message, reply_message_id?}
  GET  /app/login      -> QR (first pairing only)
  GET  /app/devices    -> connected device status

Group vs DM addressing is just the `phone` value:
  group : "123456789-987654321@g.us"
  DM    : "628123456789@s.whatsapp.net"
"""
from __future__ import annotations

import os
import time
import base64
import json
import urllib.request
import urllib.error


class RateLimiter:
    """Minimum gap between sends, with a bit of jitter-free simplicity.
    Deliberately conservative — bursts are what get accounts flagged."""
    def __init__(self, min_gap_s: float):
        self.min_gap_s = min_gap_s
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        delta = now - self._last
        if delta < self.min_gap_s:
            time.sleep(self.min_gap_s - delta)
        self._last = time.monotonic()


class GowaClient:
    def __init__(
        self,
        base_url: str | None = None,
        basic_auth: str | None = None,
        device_id: str | None = None,
        min_gap_s: float | None = None,
    ):
        self.base_url = (base_url or os.environ.get("GOWA_BASE_URL", "http://localhost:3000")).rstrip("/")
        self.basic_auth = basic_auth if basic_auth is not None else os.environ.get("GOWA_BASIC_AUTH")
        self.device_id = device_id if device_id is not None else os.environ.get("GOWA_DEVICE_ID")
        gap = min_gap_s if min_gap_s is not None else float(os.environ.get("WA_SEND_MIN_GAP_S", "4"))
        self.rate = RateLimiter(gap)

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.basic_auth:
            token = base64.b64encode(self.basic_auth.encode()).decode()
            h["Authorization"] = f"Basic {token}"
        if self.device_id:
            h["X-Device-Id"] = self.device_id
        return h

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode(),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise RuntimeError(f"gowa {path} -> HTTP {e.code}: {detail}") from e

    def send_text(self, phone: str, message: str, reply_message_id: str | None = None) -> dict:
        """Send a text to a group JID or DM JID. Rate-limited."""
        self.rate.wait()
        body: dict = {"phone": phone, "message": message}
        if reply_message_id:
            body["reply_message_id"] = reply_message_id
        return self._post("/send/message", body)

    def session_status(self) -> dict:
        """Check gowa's WhatsApp session status via GET /app/devices."""
        try:
            req = urllib.request.Request(
                f"{self.base_url}/app/devices",
                headers=self._headers(),
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode() or "{}")
                return {"ok": True, "data": data}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def as_sender(self):
        """Return a `Sender` callable (audience, text) for WAPipeline."""
        def _sender(audience: str, text: str) -> None:
            self.send_text(audience, text)
        return _sender


def make_sender(**kwargs):
    """Convenience: a rate-limited gowa Sender wired from env/kwargs."""
    return GowaClient(**kwargs).as_sender()
