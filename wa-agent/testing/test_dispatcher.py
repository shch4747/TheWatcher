import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ^ allows running from the testing/ subfolder; imports resolve to the wa-agent package

"""
Tests for the Dispatchers. Run: python test_dispatcher.py
"""
from lapis_client import FakeLapisClient
from dispatcher import LoggingDispatcher, QueueDispatcher, TRIGGERS_ROOT
from schemas import Signal, SignalType, AgentTarget


def _sig(t, summary, pid=None, sid="s1"):
    return Signal(id=sid, type=t, summary=summary, source_message_ids=["m1"],
                  group_id="g@g.us", project_ref=pid)


def test_logging_dispatcher_records():
    d = LoggingDispatcher(echo=False)
    d.trigger(AgentTarget.RESEARCH, _sig(SignalType.RESEARCH_PAPER, "paper"))
    assert d.triggers and d.triggers[0][0] == AgentTarget.RESEARCH


def test_queue_writes_trigger_record():
    c = FakeLapisClient()
    d = QueueDispatcher(c)
    d.trigger(AgentTarget.PROJECT, _sig(SignalType.PROJECT_UPDATE, "shipped x", pid="dash"))
    paths = c.list_files(f"{TRIGGERS_ROOT}/project/")
    assert len(paths) == 1
    pend = d.pending_triggers(AgentTarget.PROJECT)
    assert pend[0]["project_ref"] == "dash" and pend[0]["handled"] is False


def test_none_target_is_noop():
    c = FakeLapisClient()
    QueueDispatcher(c).trigger(AgentTarget.NONE, _sig(SignalType.QUESTION, "hmm"))
    assert c.list_files(TRIGGERS_ROOT) == []


def test_project_trigger_dedup():
    c = FakeLapisClient()
    d = QueueDispatcher(c)
    d.trigger(AgentTarget.PROJECT, _sig(SignalType.PROJECT_UPDATE, "u1", pid="dash", sid="a"))
    d.trigger(AgentTarget.PROJECT, _sig(SignalType.PROJECT_UPDATE, "u2", pid="dash", sid="b"))
    assert len(d.pending_triggers(AgentTarget.PROJECT)) == 1     # deduped

    d.trigger(AgentTarget.PROJECT, _sig(SignalType.PROJECT_UPDATE, "u3", pid="other", sid="c"))
    assert len(d.pending_triggers(AgentTarget.PROJECT)) == 2     # different project -> new


def test_mark_handled_clears_and_reopens_dedup():
    c = FakeLapisClient()
    d = QueueDispatcher(c)
    d.trigger(AgentTarget.PROJECT, _sig(SignalType.PROJECT_UPDATE, "u1", pid="dash", sid="a"))
    path = d.pending_triggers(AgentTarget.PROJECT)[0]["path"]
    d.mark_handled(path)
    assert d.pending_triggers(AgentTarget.PROJECT) == []
    # once handled, a new trigger for the same project is allowed again
    d.trigger(AgentTarget.PROJECT, _sig(SignalType.PROJECT_UPDATE, "u2", pid="dash", sid="b"))
    assert len(d.pending_triggers(AgentTarget.PROJECT)) == 1


def test_pipeline_with_queue_dispatcher_end_to_end():
    from schemas import RawMessage, MessageKind
    from lapis_adapter import LapisAdapter
    from pipeline import WAPipeline

    c = FakeLapisClient()
    pipe = WAPipeline(LapisAdapter(c), QueueDispatcher(c), allowlist={"g@g.us"})
    pipe.ingest_batch([
        RawMessage("1", "g@g.us", "Adi", 10, MessageKind.TEXT, "paper: https://arxiv.org/abs/1"),
        RawMessage("2", "g@g.us", "Adi", 20, MessageKind.TEXT, "idea: build a slack bridge"),
    ])
    assert c.list_files(f"{TRIGGERS_ROOT}/research/")
    assert c.list_files(f"{TRIGGERS_ROOT}/innovation/")


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
