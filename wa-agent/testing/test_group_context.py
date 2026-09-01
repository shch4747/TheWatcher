import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ^ allows running from the testing/ subfolder; imports resolve to the wa-agent package

"""
Tests for group-based context: registry resolution, per-group cadence,
per-group buffers, and outbound routing of agent outputs to the right group.
Run: python test_group_context.py
"""
from group_context import GroupRegistry, GroupContext
from webhook import GroupBuffers
from schemas import RawMessage, MessageKind, NotificationIntent
from memory_interface import MockWAMemory, MockDispatcher
from pipeline import WAPipeline
import mdfront

DASH = "111-111@g.us"
ML = "222-222@g.us"
TLDR = "999-999@g.us"


def _registry():
    return GroupRegistry.from_list([
        {"jid": DASH, "name": "Dashboard Team", "kind": "project",
         "projects": ["dashboard"], "n_messages": 3, "max_age_s": 60},
        {"jid": ML, "name": "ML Projects", "kind": "project", "projects": ["watcher", "rag"]},
        {"jid": TLDR, "name": "ARIES TLDR", "kind": "announce", "announce": True},
    ])


# -- registry ---------------------------------------------------------------

def test_allowlist_and_cadence():
    r = _registry()
    assert r.allowlist() == {DASH, ML, TLDR}
    assert r.cadence_for(DASH) == (3, 60)
    assert r.cadence_for("unknown@g.us") == (20, 3600.0)   # defaults


def test_resolve_project_to_group():
    r = _registry()
    assert r.resolve("project:dashboard") == [DASH]
    assert r.resolve("watcher") == [ML]                    # bare ref works too
    assert set(r.resolve("announce")) == {TLDR}


def test_resolve_jid_passthrough_and_fallback():
    r = _registry()
    assert r.resolve(DASH) == [DASH]                       # already a jid
    assert r.resolve("nonexistent-project") == [TLDR]      # unknown -> announce fallback
    assert r.resolve("") == [TLDR]                         # empty -> announce


def test_from_wiki_parses_frontmatter():
    from lapis_client import FakeLapisClient
    page = mdfront.dump({"groups": [
        {"jid": DASH, "name": "Dashboard", "projects": ["dashboard"], "n_messages": 5},
    ]}, "# Groups\n")
    c = FakeLapisClient({"meta/groups.md": page})
    r = GroupRegistry.from_wiki(c)
    assert r.get(DASH).name == "Dashboard" and r.cadence_for(DASH) == (5, 3600.0)


# -- per-group buffers ------------------------------------------------------

def test_group_buffers_respect_per_group_cadence():
    r = _registry()
    gb = GroupBuffers(registry=r)
    # DASH flushes at 3 messages; ML at default 20
    for i in range(3):
        gb.add(RawMessage(f"d{i}", DASH, "x", i))
    gb.add(RawMessage("m0", ML, "x", 1))
    ready = gb.ready_batches()
    assert len(ready) == 1 and len(ready[0]) == 3          # only DASH fired
    assert all(m.group_id == DASH for m in ready[0])


# -- outbound routing -------------------------------------------------------

def test_flush_routes_agent_output_to_project_group():
    r = _registry()
    mem = MockWAMemory()
    sent = []
    pipe = WAPipeline(mem, MockDispatcher(), allowlist=r.allowlist(),
                      sender=lambda jid, text: sent.append((jid, text)))
    # Project agent finished work on 'dashboard' and asks WA to share it.
    mem.enqueue_notification(NotificationIntent(
        id="n1", source_agent="project", audience="project:dashboard",
        payload="v0.2 shipped: gowa ingestion live"))
    n = pipe.flush_notifications(registry=r)
    assert n == 1
    assert sent[0][0] == DASH                              # routed to the right group
    assert "project" in sent[0][1] and "gowa" in sent[0][1]


def test_flush_announce_goes_to_announce_group():
    r = _registry()
    mem = MockWAMemory()
    sent = []
    pipe = WAPipeline(mem, MockDispatcher(), allowlist=r.allowlist(),
                      sender=lambda jid, text: sent.append((jid, text)))
    mem.enqueue_notification(NotificationIntent(
        id="n2", source_agent="research", audience="announce",
        payload="3 new retrieval papers this week"))
    pipe.flush_notifications(registry=r)
    assert sent and sent[0][0] == TLDR


def test_group_context_feeds_outbound(monkeypatch=None):
    # after ingest, the group's rolling context is populated and available
    r = _registry()
    mem = MockWAMemory()
    pipe = WAPipeline(mem, MockDispatcher(), allowlist=r.allowlist())
    pipe.ingest_batch([
        RawMessage("1", DASH, "Adi", 10, MessageKind.TEXT, "update: merged the parser"),
    ])
    assert "parser" in mem.get_group_context(DASH)


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
