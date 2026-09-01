"""
Inbound side of gowa: verify -> parse -> buffer -> trigger ingest.

Three pure, unit-testable pieces plus one optional Flask app:

  verify_signature(secret, raw_body, header) -> bool
      HMAC-SHA256 over the raw request body, matching gowa's
      WHATSAPP_WEBHOOK_SECRET scheme.

  parse_webhook_event(body) -> RawMessage | None
      Map gowa's v8 payload {event, device_id, payload} onto our thin
      RawMessage. Non-message events return None.

  InboxBuffer
      Durable-ish batching: append incoming RawMessages, and flush when
      >= n messages OR >= x seconds since first buffered (the trigger).

  create_app(pipeline, secret)   [needs Flask]
      A webhook receiver that ties it together.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional

from schemas import RawMessage, MessageKind


# -- signature --------------------------------------------------------------

def verify_signature(secret: str, raw_body: bytes, signature_header: str) -> bool:
    """gowa signs the raw body with HMAC-SHA256. The header may be a bare
    hex digest or prefixed (e.g. 'sha256=...'); accept both."""
    if not signature_header:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    got = signature_header.split("=", 1)[-1].strip()
    return hmac.compare_digest(expected, got)


# -- payload parsing --------------------------------------------------------

# gowa/whatsmeow message-type hints -> our normalized MessageKind.
def _detect_kind(p: dict) -> MessageKind:
    # gowa payloads vary by version; probe the common shapes defensively.
    mtype = (p.get("type") or p.get("message_type") or "").lower()
    if mtype:
        if "sticker" in mtype: return MessageKind.STICKER
        if "image" in mtype: return MessageKind.IMAGE
        if "audio" in mtype or "ptt" in mtype or "voice" in mtype: return MessageKind.VOICE
        if "document" in mtype or "file" in mtype: return MessageKind.DOCUMENT
        if "poll" in mtype: return MessageKind.POLL
        if "video" in mtype: return MessageKind.OTHER
        if "text" in mtype or "conversation" in mtype: return MessageKind.TEXT
    # structural fallbacks
    if p.get("sticker"): return MessageKind.STICKER
    if p.get("image"): return MessageKind.IMAGE
    if p.get("audio"): return MessageKind.VOICE
    if p.get("document"): return MessageKind.DOCUMENT
    if p.get("poll"): return MessageKind.POLL
    text = p.get("message") or p.get("text") or p.get("caption") or ""
    if isinstance(text, str) and text.startswith(("http://", "https://")):
        return MessageKind.LINK
    return MessageKind.TEXT if text else MessageKind.OTHER


def _first(*vals, default=None):
    for v in vals:
        if v not in (None, ""):
            return v
    return default


def parse_webhook_event(body: dict) -> Optional[RawMessage]:
    """Return a RawMessage for message events, else None. Tolerant of the
    field-name drift between gowa versions — this is the one spot to adjust
    when you confirm your gowa build's exact payload."""
    if body.get("event") not in (None, "message", "message.text", "message.any"):
        # only handle message-ish events; ignore acks/presence/etc.
        if "message" not in str(body.get("event", "")):
            return None

    p = body.get("payload") or body
    msg_id = _first(p.get("id"), p.get("message_id"), (p.get("key") or {}).get("id"))
    group_id = _first(
        p.get("chat_id"), p.get("from"), p.get("remote_jid"),
        (p.get("key") or {}).get("remote_jid"),
    )
    if not msg_id or not group_id:
        return None

    sender = _first(
        p.get("pushname"), p.get("sender"), p.get("participant"),
        (p.get("key") or {}).get("participant"), group_id,
    )
    ts = p.get("timestamp") or p.get("t") or time.time()
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        ts = time.time()

    kind = _detect_kind(p)
    text = _first(p.get("message"), p.get("text"), p.get("caption"), default="")
    if isinstance(text, dict):  # some builds nest {message:{text:...}}
        text = _first(text.get("text"), text.get("conversation"), default="")

    quoted = _first(
        p.get("reply_message_id"), p.get("quoted_message_id"),
        ((p.get("context") or {}).get("quoted_message_id")),
    )

    return RawMessage(
        id=str(msg_id),
        group_id=str(group_id),
        sender=str(sender),
        timestamp=ts,
        kind=kind,
        text=text if isinstance(text, str) else "",
        quoted_id=str(quoted) if quoted else None,
        media_ref=_first(p.get("media_path"), p.get("url"), p.get("media_url")),
        duration_s=p.get("seconds") or p.get("duration"),
        filename=p.get("filename") or p.get("file_name"),
        raw=body,
    )


# -- buffering + trigger ----------------------------------------------------

class InboxBuffer:
    """Accumulate messages and decide when to flush a batch to the pipeline.
    Trigger: >= max_messages buffered, OR >= max_age_s since the first one."""

    def __init__(self, max_messages: int = 20, max_age_s: float = 3600.0):
        self.max_messages = max_messages
        self.max_age_s = max_age_s
        self._buf: list[RawMessage] = []
        self._first_at: float | None = None

    def add(self, msg: RawMessage) -> None:
        if not self._buf:
            self._first_at = time.monotonic()
        self._buf.append(msg)

    def should_flush(self, now: float | None = None) -> bool:
        if not self._buf:
            return False
        if len(self._buf) >= self.max_messages:
            return True
        now = now if now is not None else time.monotonic()
        return (now - (self._first_at or now)) >= self.max_age_s

    def drain(self) -> list[RawMessage]:
        batch, self._buf, self._first_at = self._buf, [], None
        return batch


class GroupBuffers:
    """One InboxBuffer per group, each with that group's own cadence (the
    'N messages / X hours, personalizable per group' requirement). A
    GroupRegistry supplies per-group cadence; unknown groups get defaults."""

    def __init__(self, registry=None, default_n=20, default_age_s=3600.0):
        self.registry = registry
        self.default_n = default_n
        self.default_age_s = default_age_s
        self._buffers: dict[str, InboxBuffer] = {}

    def _buffer_for(self, group_id: str) -> InboxBuffer:
        if group_id not in self._buffers:
            if self.registry:
                n, age = self.registry.cadence_for(group_id)
            else:
                n, age = self.default_n, self.default_age_s
            self._buffers[group_id] = InboxBuffer(max_messages=n, max_age_s=age)
        return self._buffers[group_id]

    def add(self, msg: RawMessage) -> None:
        self._buffer_for(msg.group_id).add(msg)

    def ready_batches(self, now: float | None = None) -> list[list[RawMessage]]:
        """Drain and return a batch for every group whose trigger has fired."""
        out = []
        for buf in self._buffers.values():
            if buf.should_flush(now):
                out.append(buf.drain())
        return out


# -- optional Flask receiver ------------------------------------------------

def create_app(pipeline, secret: str, buffers: "GroupBuffers | None" = None, registry=None):
    """Build a Flask app: POST /webhook verifies, parses, buffers per group,
    and flushes each group's batch to pipeline.ingest_batch when that group's
    trigger fires. Import Flask lazily so the package has no web dependency."""
    from flask import Flask, request

    app = Flask(__name__)
    bufs = buffers or GroupBuffers(registry=registry)

    def _flush_ready():
        for batch in bufs.ready_batches():
            pipeline.ingest_batch(batch)

    @app.post("/webhook")
    def webhook():
        raw = request.get_data()
        sig = request.headers.get("X-Hub-Signature-256") or request.headers.get("X-Signature") or ""
        if secret and not verify_signature(secret, raw, sig):
            return {"error": "bad signature"}, 401
        msg = parse_webhook_event(request.get_json(force=True, silent=True) or {})
        if msg:
            bufs.add(msg)
        _flush_ready()
        return {"ok": True}

    @app.post("/flush")   # manual trigger for testing: flush ALL groups now
    def flush():
        total = 0
        for buf in bufs._buffers.values():
            if buf._buf:
                total += pipeline.ingest_batch(buf.drain()).threads
        return {"flushed_threads": total}

    return app
