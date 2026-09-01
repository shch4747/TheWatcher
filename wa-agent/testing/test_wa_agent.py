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
    assert router.route(sig(SignalType.PROJECT_UPDATE)).agent_target == AgentTarget.PROJECT
    assert router.route(sig(SignalType.EVENT_INFO)).wiki_method == "upsert_event"
    assert router.route(sig(SignalType.RESEARCH_PAPER)).wiki_method == "append_research_mention"


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


def test_seen_prevents_reprocessing():
    memory, dispatcher = MockWAMemory(), MockDispatcher()
    pipe = WAPipeline(memory, dispatcher, allowlist={G})
    raw = [RawMessage("2", G, "Adi", 20, MessageKind.TEXT, "update: shipped thing")]
    pipe.ingest_batch(raw)
    first = len(dispatcher.triggers)
    pipe.ingest_batch(raw)                 # same batch again
    assert len(dispatcher.triggers) == first   # nothing re-triggered


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
