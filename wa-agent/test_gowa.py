"""
Tests for the gowa integration: signature verification, webhook payload
parsing, buffer trigger logic, the rate limiter, and the reminder builder.
Run:  python test_gowa.py   (or python -m pytest -q test_gowa.py)
"""
import hashlib
import hmac
import time

from schemas import MessageKind
from webhook import verify_signature, parse_webhook_event, InboxBuffer
from gowa_client import RateLimiter
from memory_interface import MockWAMemory
import reminders

G = "123-456@g.us"


# -- signature --------------------------------------------------------------

def test_signature_roundtrip_and_prefix():
    secret, body = "s3cret", b'{"event":"message"}'
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(secret, body, digest) is True
    assert verify_signature(secret, body, f"sha256={digest}") is True
    assert verify_signature(secret, body, "deadbeef") is False
    assert verify_signature(secret, body, "") is False


# -- payload parsing --------------------------------------------------------

def test_parse_text_message():
    body = {"event": "message", "device_id": "d@s.whatsapp.net",
            "payload": {"id": "m1", "chat_id": G, "pushname": "Adi",
                        "timestamp": 1700000000, "type": "text", "message": "hello team"}}
    m = parse_webhook_event(body)
    assert m and m.id == "m1" and m.group_id == G and m.sender == "Adi"
    assert m.kind == MessageKind.TEXT and m.text == "hello team"


def test_parse_voice_and_sticker_kinds():
    voice = parse_webhook_event({"event": "message", "payload": {
        "id": "v1", "chat_id": G, "type": "audio/ptt", "seconds": 42}})
    assert voice.kind == MessageKind.VOICE and voice.duration_s == 42
    sticker = parse_webhook_event({"event": "message", "payload": {
        "id": "s1", "chat_id": G, "type": "sticker"}})
    assert sticker.kind == MessageKind.STICKER


def test_parse_link_detected_from_text():
    m = parse_webhook_event({"event": "message", "payload": {
        "id": "l1", "chat_id": G, "message": "https://arxiv.org/abs/1"}})
    assert m.kind == MessageKind.LINK


def test_parse_nested_key_shape():
    # some builds nest id/remote_jid under "key"
    m = parse_webhook_event({"event": "message", "payload": {
        "key": {"id": "k1", "remote_jid": G, "participant": "Riya"},
        "message": "nested shape", "timestamp": 1700000001}})
    assert m and m.id == "k1" and m.group_id == G and m.sender == "Riya"


def test_non_message_event_ignored():
    assert parse_webhook_event({"event": "presence", "payload": {"id": "x"}}) is None
    assert parse_webhook_event({"event": "message", "payload": {}}) is None  # no id/chat


# -- buffer -----------------------------------------------------------------

def test_buffer_flushes_on_count():
    from schemas import RawMessage
    buf = InboxBuffer(max_messages=3, max_age_s=9999)
    assert not buf.should_flush()
    for i in range(2):
        buf.add(RawMessage(str(i), G, "x", 1))
    assert not buf.should_flush()
    buf.add(RawMessage("2", G, "x", 1))
    assert buf.should_flush()
    assert len(buf.drain()) == 3
    assert not buf.should_flush()   # emptied


def test_buffer_flushes_on_age():
    from schemas import RawMessage
    buf = InboxBuffer(max_messages=99, max_age_s=100)
    buf.add(RawMessage("0", G, "x", 1))
    buf._first_at = time.monotonic() - 200      # pretend 200s elapsed
    assert buf.should_flush() is True


# -- rate limiter -----------------------------------------------------------

def test_rate_limiter_enforces_gap():
    rl = RateLimiter(min_gap_s=0.05)
    rl.wait()                       # first is immediate
    start = time.monotonic()
    rl.wait()                       # second must wait ~0.05s
    assert time.monotonic() - start >= 0.04


# -- reminders --------------------------------------------------------------

def test_build_reminders_from_wiki_reads():
    mem = MockWAMemory(
        upcoming_events=[{"id": "e1", "title": "RAG workshop", "date": "Fri 5pm",
                          "location": "LHC", "group": G}],
        pending_tasks=[{"title": "wire gowa"}, {"title": "write LapisAdapter"}],
        stale_projects=[{"id": "p1", "title": "dashboard", "days_stale": 9, "group": G}],
    )
    intents = reminders.build_reminders(mem, default_audience="tldr@g.us")
    kinds = {i.source_agent for i in intents}
    assert kinds == {"events", "tasks", "project"}
    ev = next(i for i in intents if i.source_agent == "events")
    assert "RAG workshop" in ev.payload and ev.audience == G


def test_run_reminder_job_enqueues():
    mem = MockWAMemory(pending_tasks=[{"title": "t1"}])
    n = reminders.run_reminder_job(mem, default_audience="tldr@g.us")
    assert n == 1 and len(mem.poll_pending_notifications()) == 1


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:
            failed += 1; print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
