import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ^ allows running from the testing/ subfolder; imports resolve to the wa-agent package

"""
Unit tests for the deterministic parts of the WA agent — the stuff that
must be correct regardless of what the LLM does. Run:  python -m pytest -q
(or just `python test_wa_agent.py` for a dependency-free run).
"""
from datetime import datetime, timezone

from schemas import RawMessage, MessageKind, NormalizedMessage, SignalType, AgentTarget
from media import StubMediaProcessor
import preprocessing as pp
import router
from memory_interface import MockWAMemory, MockDispatcher
from pipeline import WAPipeline

G = "proj@g.us"
MEDIA = StubMediaProcessor()


def _nm(id, text, ts=0, quoted=None, kind=MessageKind.TEXT):
    return NormalizedMessage(id=id, group_id=G, sender="x",
                             ts=datetime.fromtimestamp(ts, tz=timezone.utc),
                             kind=kind, text=text, quoted_id=quoted)


# -- stage 1: dedup ---------------------------------------------------------

def test_dedup_drops_seen_and_duplicates_and_sorts():
    msgs = [
        RawMessage("b", G, "x", 20), RawMessage("a", G, "x", 10),
        RawMessage("a", G, "x", 10), RawMessage("seen", G, "x", 5),
    ]
    out = pp.dedup_and_order(msgs, seen_ids={"seen"})
    assert [m.id for m in out] == ["a", "b"]


# -- stage 2: allowlist -----------------------------------------------------

def test_filter_groups():
    msgs = [RawMessage("1", "good@g.us", "x", 1), RawMessage("2", "bad@g.us", "x", 2)]
    out = pp.filter_groups(msgs, {"good@g.us"})
    assert [m.id for m in out] == ["1"]


# -- stage 3: normalize -----------------------------------------------------

def test_sticker_and_system_dropped():
    assert pp.normalize(RawMessage("s", G, "x", 1, MessageKind.STICKER), MEDIA) is None
    assert pp.normalize(RawMessage("y", G, "x", 1, MessageKind.SYSTEM), MEDIA) is None


def test_image_becomes_text_standin():
    nm = pp.normalize(RawMessage("i", G, "x", 1, MessageKind.IMAGE, text="error log"), MEDIA)
    assert nm is not None and "error log" in nm.text


def test_voice_reply_flagged_important():
    nm = pp.normalize(RawMessage("v", G, "x", 1, MessageKind.VOICE, quoted_id="m0", duration_s=30), MEDIA)
    assert nm.needs_human_tldr is True
    assert "voice note" in nm.text


def test_voice_standalone_not_flagged():
    nm = pp.normalize(RawMessage("v", G, "x", 1, MessageKind.VOICE, duration_s=5), MEDIA)
    assert nm.needs_human_tldr is False


# -- stage 4: prefilter -----------------------------------------------------

def test_noise_is_dropped_but_signal_kept():
    assert pp.is_noise(_nm("1", "ok")) is True
    assert pp.is_noise(_nm("2", "thanks")) is True
    assert pp.is_noise(_nm("3", "deadline is tomorrow 5pm")) is False
    assert pp.is_noise(_nm("4", "https://arxiv.org/abs/1")) is False


def test_unresolved_media_never_noise():
    nm = _nm("v", "[voice note from x, 30s]")
    nm.needs_human_tldr = True
    assert pp.is_noise(nm) is False


# -- stage 5: thread assembly ----------------------------------------------

def test_threads_split_on_time_gap():
    msgs = [_nm("a", "hello there team", 0), _nm("b", "another real message", 100),
            _nm("c", "much later message here", 100000)]
    threads = pp.assemble_threads(msgs, gap_seconds=900)
    assert len(threads) == 2


def test_reply_keeps_thread_together_across_gap():
    msgs = [_nm("a", "first message here", 0),
            _nm("b", "reply much later", 100000, quoted="a")]
    threads = pp.assemble_threads(msgs, gap_seconds=900)
    assert len(threads) == 1


# -- routing ----------------------------------------------------------------

def test_routing_rule_matches_club_spec():
    from schemas import Signal

    def sig(t):
        return Signal(id="1", type=t, summary="", source_message_ids=[], group_id=G)

    assert router.route(sig(SignalType.RESEARCH_PAPER)).agent_target == AgentTarget.RESEARCH
    assert router.route(sig(SignalType.PROJECT_IDEA)).agent_target == AgentTarget.INNOVATION
    assert router.route(sig(SignalType.EVENT_IDEA)).agent_target == AgentTarget.INNOVATION
    assert router.route(sig(SignalType.FEEDBACK)).agent_target == AgentTarget.INNOVATION
    assert router.route(sig(SignalType.PROJECT_UPDATE)).agent_target == AgentTarget.PROJECT
    assert router.route(sig(SignalType.EVENT_INFO)).wiki_method == "upsert_event"
    assert router.route(sig(SignalType.RESEARCH_PAPER)).wiki_method == "append_research_mention"
    assert router.route(sig(SignalType.FEEDBACK)).wiki_method == "record_feedback"
    assert router.route(sig(SignalType.MOM)).wiki_method == "write_mom"
    assert router.route(sig(SignalType.USER_PING)).wiki_method == "write_user_ping"
    assert router.route(sig(SignalType.MOM)).should_notify is True


# -- end to end (stub classifier) ------------------------------------------

def test_pipeline_ingest_end_to_end():
    memory, dispatcher = MockWAMemory(), MockDispatcher()
    sent = []
    pipe = WAPipeline(memory, dispatcher, allowlist={G},
                      sender=lambda a, t: sent.append((a, t)))
    raw = [
        RawMessage("1", G, "Adi", 10, MessageKind.TEXT, "ok"),
        RawMessage("2", G, "Adi", 20, MessageKind.TEXT,
                   "update: merged the ingestion layer"),
        RawMessage("3", G, "Riya", 30, MessageKind.TEXT,
                   "paper: https://arxiv.org/abs/2401.1"),
        RawMessage("bad", "other@g.us", "x", 40, MessageKind.TEXT, "ignored"),
    ]
    res = pipe.ingest_batch(raw)
    targets = {t for t, _ in dispatcher.triggers}
    assert AgentTarget.PROJECT in targets
    assert AgentTarget.RESEARCH in targets
    # dedup/allowlist: the other-group message never got processed or seen
    assert "bad" not in res.processed_ids
    assert memory.is_seen("2") and not memory.is_seen("bad")


def test_system_join_not_dropped():
    """System messages about joins should survive preprocessing (for welcome context)."""
    raw = [RawMessage("j1", G, "sys", 10, MessageKind.SYSTEM, "Adi joined using this group's invite link")]
    threads = pp.preprocess(raw, {G}, set())
    assert len(threads) == 1
    assert threads[0].messages[0].text.startswith("[join]")


def test_system_leave_still_dropped():
    """Non-join system messages should still be dropped."""
    raw = [RawMessage("l1", G, "sys", 10, MessageKind.SYSTEM, "Riya left")]
    threads = pp.preprocess(raw, {G}, set())
    assert len(threads) == 0


def test_join_triggers_welcome_with_context():
    """When someone joins and group has context, a welcome message is sent."""
    memory, dispatcher = MockWAMemory(), MockDispatcher()
    memory.update_group_context(G, "working on dashboard rewrite")
    sent = []
    pipe = WAPipeline(memory, dispatcher, allowlist={G},
                      sender=lambda a, t: sent.append((a, t)))
    raw = [RawMessage("j1", G, "sys", 10, MessageKind.SYSTEM, "Adi was added")]
    pipe.ingest_batch(raw)
    welcome_msgs = [t for a, t in sent if "Welcome" in t]
    assert len(welcome_msgs) >= 1
    assert "dashboard rewrite" in welcome_msgs[0]


def test_feedback_routes_to_innovation():
    """Feedback signal should route to Innovation agent."""
    from llm_client import _guess_type
    assert _guess_type("feedback: the hackathon went well but venue was too small") == SignalType.FEEDBACK


def test_mom_classified_correctly():
    """MoM keywords should classify as MOM signal."""
    from llm_client import _guess_type
    assert _guess_type("meeting notes: decided to ship v2 next week") == SignalType.MOM


def test_user_ping_classified_correctly():
    """User ping keywords should classify as USER_PING signal."""
    from llm_client import _guess_type
    assert _guess_type("@bot note this: next meetup is on Friday") == SignalType.USER_PING


def test_group_context_passed_to_classifier():
    """Pipeline should pass group context to the classifier."""
    received_ctx = []
    def mock_classifier(thread, group_context=""):
        received_ctx.append(group_context)
        return []
    memory, dispatcher = MockWAMemory(), MockDispatcher()
    memory.update_group_context(G, "discussing ML pipelines")
    pipe = WAPipeline(memory, dispatcher, allowlist={G}, classifier=mock_classifier)
    raw = [RawMessage("1", G, "Adi", 10, MessageKind.TEXT, "update: shipped the parser")]
    pipe.ingest_batch(raw)
    assert len(received_ctx) >= 1
    assert "ML pipelines" in received_ctx[0]


def test_seen_prevents_reprocessing():
    memory, dispatcher = MockWAMemory(), MockDispatcher()
    pipe = WAPipeline(memory, dispatcher, allowlist={G})
    raw = [RawMessage("2", G, "Adi", 20, MessageKind.TEXT, "update: shipped thing")]
    pipe.ingest_batch(raw)
    first = len(dispatcher.triggers)
    pipe.ingest_batch(raw)                 # same batch again
    assert len(dispatcher.triggers) == first   # nothing re-triggered


def test_noisy_group_heavier_filtering():
    """In noisy groups (coordi/exes), short casual messages are dropped even if
    they'd survive the normal 3-word threshold."""
    NOISY = "noisy@g.us"
    # "yeah sounds good" is 3 words — survives normal but dropped in noisy
    raw = [
        RawMessage("n1", NOISY, "x", 10, MessageKind.TEXT, "yeah sounds good"),
        RawMessage("n2", NOISY, "x", 20, MessageKind.TEXT, "update: merged the ingestion layer"),
    ]
    # Without noisy flag: both survive (3 words >= _MIN_KEEP_WORDS=3)
    threads_normal = pp.preprocess(raw, {NOISY}, set(), noisy_jids=set())
    normal_texts = [m.text for t in threads_normal for m in t.messages]
    assert any("merged" in t for t in normal_texts)

    # With noisy flag: short casual message dropped, real signal kept
    threads_noisy = pp.preprocess(raw, {NOISY}, set(), noisy_jids={NOISY})
    noisy_texts = [m.text for t in threads_noisy for m in t.messages]
    assert any("merged" in t for t in noisy_texts)
    assert not any("yeah" in t for t in noisy_texts)


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
