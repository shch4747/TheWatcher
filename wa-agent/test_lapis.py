"""
Tests for LapisAdapter against FakeLapisClient — exercises the real
read/write/frontmatter logic with no network or token. Run:
    python test_lapis.py   (or python -m pytest -q test_lapis.py)
"""
from datetime import datetime, timezone, timedelta

import mdfront
from lapis_client import FakeLapisClient
from lapis_adapter import LapisAdapter, LAYOUT, INPUT_PAGES, _slug
from schemas import Signal, SignalType, NotificationIntent

G = "123-456@g.us"


def _sig(t, summary, project_ref=None, payload=None, sid="ab12"):
    return Signal(id=sid, type=t, summary=summary, source_message_ids=["m1"],
                  group_id=G, project_ref=project_ref, payload=payload or {})


def _fresh():
    c = FakeLapisClient()
    return c, LapisAdapter(c)


# -- frontmatter roundtrip --------------------------------------------------

def test_frontmatter_roundtrip():
    text = mdfront.dump({"type": "task", "tags": ["a", "b"], "status": "open"}, "# Body\n")
    meta, body = mdfront.parse(text)
    assert meta["type"] == "task" and meta["tags"] == ["a", "b"]
    assert "# Body" in body


# -- dedup state ------------------------------------------------------------

def test_seen_persists_to_a_note():
    c, mem = _fresh()
    assert not mem.is_seen("m1")
    mem.mark_seen(["m1", "m2"])
    assert mem.is_seen("m1") and mem.is_seen("m2")
    # persisted as JSON in the state file, and a fresh adapter re-reads it
    mem2 = LapisAdapter(c)
    assert mem2.is_seen("m2") and not mem2.is_seen("m3")


# -- writeback --------------------------------------------------------------

def test_research_mention_appends_to_research_digest_page():
    c, mem = _fresh()
    mem.append_research_mention(_sig(SignalType.RESEARCH_PAPER, "Self-RAG paper",
                                     payload={"url": "https://arxiv.org/abs/1"}))
    body = c.read_file(INPUT_PAGES["research"])   # WhatsApp/ResearchDigest.md
    assert body and "Self-RAG" in body and "https://arxiv.org/abs/1" in body


def test_input_queue_consume_clears():
    c, mem = _fresh()
    mem.append_research_mention(_sig(SignalType.RESEARCH_PAPER, "paper one"))
    mem.append_research_mention(_sig(SignalType.RESEARCH_PAPER, "paper two", sid="cd34"))
    assert len(mem.read_agent_input("research")) == 2
    consumed = mem.consume_agent_input("research")
    assert len(consumed) == 2
    assert mem.read_agent_input("research") == []           # queue cleared


def test_ideas_append_to_inbox():
    c, mem = _fresh()
    mem.record_idea(_sig(SignalType.PROJECT_IDEA, "slack bridge", project_ref="watcher"))
    body = c.read_file(INPUT_PAGES["innovation"])            # Ideas/Inbox.md
    assert body and "slack bridge" in body and "watcher" in body


def test_project_update_appends_and_bumps_last_update():
    c, mem = _fresh()
    s = _sig(SignalType.PROJECT_UPDATE, "merged webhook receiver", project_ref="dashboard")
    mem.append_project_update(s)
    mem.append_project_update(_sig(SignalType.PROJECT_UPDATE, "added rate limiter", project_ref="dashboard"))
    path = f"{LAYOUT['projects']}/dashboard.md"
    meta, body = mdfront.parse(c.read_file(path))
    assert meta["type"] == "project" and meta["last_update"]
    # both updates in the one page, single Updates section
    assert body.count("## Updates") == 1
    assert "merged webhook receiver" in body and "added rate limiter" in body


def test_event_and_task_upsert():
    c, mem = _fresh()
    mem.upsert_event(_sig(SignalType.EVENT_INFO, "RAG workshop",
                          payload={"date": "2026-08-28", "location": "LHC"}))
    mem.upsert_task(_sig(SignalType.TASK, "write LapisAdapter", project_ref="watcher"))
    ev = c.list_files(LAYOUT["events"])[0]
    tk = c.list_files(LAYOUT["tasks"])[0]
    assert mdfront.parse(c.read_file(ev))[0]["date"] == "2026-08-28"
    assert mdfront.parse(c.read_file(tk))[0]["status"] == "open"


def test_chat_digest_accumulates_in_one_dated_note():
    c, mem = _fresh()
    mem.write_chat_digest(G, "decided to use gowa", ["m1"])
    mem.write_chat_digest(G, "picked kimi k2.6", ["m2"])
    paths = c.list_files(LAYOUT["channels"])
    assert len(paths) == 1                        # same group + day -> one note
    body = c.read_file(paths[0])
    assert "gowa" in body and "kimi" in body


# -- reminder reads ---------------------------------------------------------

def test_upcoming_events_filtered_by_window():
    c, mem = _fresh()
    soon = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    far = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
    c.write_file(f"{LAYOUT['events']}/a.md", mdfront.dump(
        {"type": "event", "date": soon, "status": "upcoming"}, "# Soon event\n"))
    c.write_file(f"{LAYOUT['events']}/b.md", mdfront.dump(
        {"type": "event", "date": far, "status": "upcoming"}, "# Far event\n"))
    within = mem.get_upcoming_events(within_days=3)
    titles = {e["title"] for e in within}
    assert "a" in titles and "b" not in titles


def test_pending_tasks_excludes_done():
    c, mem = _fresh()
    c.write_file(f"{LAYOUT['tasks']}/t1.md", mdfront.dump({"type": "task", "status": "open"}, "# T1\n"))
    c.write_file(f"{LAYOUT['tasks']}/t2.md", mdfront.dump({"type": "task", "status": "done"}, "# T2\n"))
    pending = mem.get_pending_tasks()
    assert [p["title"] for p in pending] == ["t1"]


def test_projects_needing_nudge():
    c, mem = _fresh()
    old = (datetime.now(timezone.utc) - timedelta(days=20)).date().isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    c.write_file(f"{LAYOUT['projects']}/stale.md", mdfront.dump(
        {"type": "project", "status": "active", "last_update": old}, "# stale\n"))
    c.write_file(f"{LAYOUT['projects']}/fresh.md", mdfront.dump(
        {"type": "project", "status": "active", "last_update": recent}, "# fresh\n"))
    nudge = mem.get_projects_needing_nudge(stale_days=7)
    assert [n["title"] for n in nudge] == ["stale"]
    assert nudge[0]["days_stale"] >= 20


# -- notification queue -----------------------------------------------------

def test_notification_queue_roundtrip():
    c, mem = _fresh()
    mem.enqueue_notification(NotificationIntent(
        id="n1", source_agent="research", audience=G, payload="3 new papers"))
    pending = mem.poll_pending_notifications()
    assert len(pending) == 1 and pending[0].payload == "3 new papers"
    mem.mark_notification_sent("n1")
    assert mem.poll_pending_notifications() == []
    # the note is retained, just marked sent
    meta, _ = mdfront.parse(c.read_file(f"{LAYOUT['notifications']}/n1.md"))
    assert meta["sent"] is True


# -- full inbound pipeline against the real adapter -------------------------

def test_pipeline_writes_through_lapis_adapter():
    from schemas import RawMessage, MessageKind
    from memory_interface import MockDispatcher
    from pipeline import WAPipeline

    c, mem = _fresh()
    pipe = WAPipeline(mem, MockDispatcher(), allowlist={G})
    pipe.ingest_batch([
        RawMessage("1", G, "Adi", 20, MessageKind.TEXT, "update: shipped the parser"),
        RawMessage("2", G, "Riya", 30, MessageKind.TEXT, "paper: https://arxiv.org/abs/9"),
    ])
    assert c.list_files(LAYOUT["projects"])            # project page written
    assert c.read_file(INPUT_PAGES["research"])         # research input page appended
    assert mem.is_seen("1")
    assert mem.get_group_context(G)                     # rolling context updated


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
