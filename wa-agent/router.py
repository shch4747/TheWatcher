"""
Routing: a classified Signal -> a declarative RoutingDecision.

This is where the club's routing rule lives, in one place:
  - research papers found in chat  -> Research Agent
  - project / event ideas          -> Innovation Agent
  - project updates                -> Project Agent
  - events / tasks / decisions / announcements / questions -> the wiki
    (events also feed the reminder job)

The router only DECIDES. The pipeline executes the decision (writes to the
wiki, triggers the agent, enqueues a notification), which keeps this pure
and unit-testable.
"""
from schemas import Signal, SignalType, AgentTarget, RoutingDecision, SIGNAL_TO_AGENT


# Which wiki write (a WAMemory method name) each signal type triggers.
_SIGNAL_TO_WIKI: dict[SignalType, str | None] = {
    SignalType.RESEARCH_PAPER: "append_research_mention",
    SignalType.PROJECT_IDEA: "record_idea",
    SignalType.EVENT_IDEA: "record_idea",
    SignalType.PROJECT_UPDATE: "append_project_update",
    SignalType.EVENT_INFO: "upsert_event",
    SignalType.TASK: "upsert_task",
    SignalType.DECISION: "write_chat_digest",
    SignalType.ANNOUNCEMENT: "write_chat_digest",
    SignalType.QUESTION: "write_chat_digest",
    SignalType.NOISE: None,
}

# Signal types worth echoing back out to WhatsApp as a notification.
_NOTIFY_TYPES = {SignalType.EVENT_INFO, SignalType.ANNOUNCEMENT, SignalType.DECISION}


def route(signal: Signal) -> RoutingDecision:
    return RoutingDecision(
        signal=signal,
        wiki_method=_SIGNAL_TO_WIKI.get(signal.type),
        agent_target=SIGNAL_TO_AGENT.get(signal.type, AgentTarget.NONE),
        should_notify=signal.type in _NOTIFY_TYPES,
    )
