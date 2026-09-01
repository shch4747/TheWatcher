"""
End-to-end demo of the WA Agent's inbound + outbound paths against mocks —
no gowa, no Lapis, no LLM key required.

Run:  python demo.py
With a real classifier:  export OPENROUTER_API_KEY=... && python demo.py
"""
from schemas import RawMessage, MessageKind, NotificationIntent, Urgency
from memory_interface import MockWAMemory, MockDispatcher
from pipeline import WAPipeline

PROJECT_GROUP = "dashboard-team@g.us"
ANNOUNCE_GROUP = "aries-tldr@g.us"
RANDOM_GROUP = "memes@g.us"                # not allowlisted -> ignored
ALLOWLIST = {PROJECT_GROUP, ANNOUNCE_GROUP}


def fake_inbox() -> list[RawMessage]:
    t = 1_700_000_000
    return [
        RawMessage("m1", PROJECT_GROUP, "Adi", t + 10, MessageKind.TEXT,
                   "ok"),                                              # noise
        RawMessage("m2", PROJECT_GROUP, "Adi", t + 20, MessageKind.TEXT,
                   "update: merged the gowa webhook receiver, ingestion works now"),  # -> project
        RawMessage("m3", PROJECT_GROUP, "Riya", t + 40, MessageKind.TEXT,
                   "cool paper on WA automation: https://arxiv.org/abs/2401.12345"),  # -> research
        RawMessage("m4", PROJECT_GROUP, "Riya", t + 60, MessageKind.TEXT,
                   "idea: we should build a Slack bridge for the same agent"),        # -> innovation
        RawMessage("m5", PROJECT_GROUP, "Kabir", t + 90, MessageKind.VOICE,
                   "", quoted_id="m4", duration_s=42),                 # important voice -> clarify
        RawMessage("m6", ANNOUNCE_GROUP, "Core", t + 120, MessageKind.TEXT,
                   "event: RAG workshop this Friday 5pm in LHC, RSVP here"),          # -> event + notify
        RawMessage("m7", PROJECT_GROUP, "Adi", t + 150, MessageKind.STICKER,
                   ""),                                                # dropped
        RawMessage("m8", RANDOM_GROUP, "Someone", t + 160, MessageKind.TEXT,
                   "totally important but wrong group"),               # dropped (not allowlisted)
        RawMessage("m9", PROJECT_GROUP, "Adi", t + 900, MessageKind.IMAGE,
                   "screenshot: TypeError in pipeline.py line 42", media_ref="img1"),  # image -> ocr text
    ]


def main():
    memory = MockWAMemory()
    dispatcher = MockDispatcher()
    sent: list[tuple[str, str]] = []
    pipe = WAPipeline(
        memory=memory, dispatcher=dispatcher, allowlist=ALLOWLIST,
        sender=lambda audience, text: sent.append((audience, text)),
    )

    print("=" * 70)
    print("INBOUND: ingesting a fake WhatsApp batch")
    print("=" * 70)
    result = pipe.ingest_batch(fake_inbox())
    print(f"threads assembled : {result.threads}")
    print(f"signals extracted : {len(result.signals)}")
    print()
    for d in result.decisions:
        s = d.signal
        print(f"  • {s.type.value:<16} agent={d.agent_target.value:<11} "
              f"wiki={d.wiki_method or '-':<22} :: {s.summary[:50]}")

    print("\nwiki writes:")
    for method, arg in memory.writes:
        label = arg if isinstance(arg, str) else arg.summary[:50]
        print(f"  - {method}: {label}")

    print("\nagent triggers:")
    for target, s in dispatcher.triggers:
        print(f"  - {target.value:<11} <- {s.type.value} ({s.summary[:40]})")

    print("\nclarifying questions sent to chat:")
    for q in result.clarifications:
        print(f"  ? {q}")

    print("\n" + "=" * 70)
    print("OUTBOUND: another agent enqueues a notification, WA agent flushes it")
    print("=" * 70)
    memory.enqueue_notification(NotificationIntent(
        id="n1", source_agent="research", audience=ANNOUNCE_GROUP,
        payload="3 new papers on retrieval agents this week — top: 'Self-RAG'.",
        urgency=Urgency.NORMAL,
    ))
    n = pipe.flush_notifications()
    print(f"notifications flushed: {n}")

    print("\nall messages the WA agent sent to WhatsApp:")
    for audience, text in sent:
        print(f"  -> {audience}: {text}")


if __name__ == "__main__":
    main()
