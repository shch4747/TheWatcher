"""
The WA Agent pipeline — the one module that knows about all the pieces.

Inbound (chat -> wiki + agent triggers):

    ingest_batch(raw_messages)
        -> preprocess (dedup, allowlist, normalize, prefilter, threads)
        -> classify each thread into Signals
        -> route each Signal
        -> execute: write to wiki, trigger target agent, maybe enqueue a
           WhatsApp notification, ask clarifying questions
        -> mark ids seen (only after success)

Outbound (other agents -> WhatsApp):

    flush_notifications()
        -> drain the notification queue, summarise + send via a Sender

The Sender and clarifier are injected so the whole thing runs against
mocks with no gowa. Everything is stubbed but the control flow is real.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from schemas import (
    RawMessage, Signal, SignalType, RoutingDecision, AgentTarget, NotificationIntent,
    MessageKind,
)
from memory_interface import WAMemory, Dispatcher
from media import MediaProcessor
import preprocessing
import router
from llm_client import classify_thread


# A Sender turns a finished message string + audience into an actual gowa
# send. Injected so tests use a recorder and prod uses the gowa client.
Sender = Callable[[str, str], None]      # (audience, text) -> None
Classifier = Callable[..., list[Signal]]   # (Thread, group_context=...) -> [Signal]


@dataclass
class IngestResult:
    threads: int = 0
    signals: list[Signal] = field(default_factory=list)
    decisions: list[RoutingDecision] = field(default_factory=list)
    clarifications: list[str] = field(default_factory=list)
    processed_ids: list[str] = field(default_factory=list)


class WAPipeline:
    def __init__(
        self,
        memory: WAMemory,
        dispatcher: Dispatcher,
        allowlist: set[str],
        sender: Optional[Sender] = None,
        media: Optional[MediaProcessor] = None,
        classifier: Classifier = classify_thread,
        noisy_jids: set[str] | None = None,
    ):
        self.memory = memory
        self.dispatcher = dispatcher
        self.allowlist = allowlist
        self.sender = sender or (lambda audience, text: None)
        self.media = media
        self.classify = classifier
        self.noisy_jids = noisy_jids or set()

    # -- inbound ---------------------------------------------------------

    def ingest_batch(self, raw: list[RawMessage]) -> IngestResult:
        result = IngestResult()
        seen = {m.id for m in raw if self.memory.is_seen(m.id)}
        threads = preprocessing.preprocess(raw, self.allowlist, seen, self.media,
                                           noisy_jids=self.noisy_jids)
        result.threads = len(threads)

        for thread in threads:
            # Handle join events: send newcomers a welcome with group context
            self._handle_joins(thread)

            # Feed group context INTO classification (design: "send to LLM
            # along with group context") so the model knows what the group
            # has been discussing when it classifies new messages.
            group_ctx = self.memory.get_group_context(thread.group_id)
            decisions = [router.route(s) for s in self.classify(thread, group_context=group_ctx)]
            # WA triggers Project agent BEFORE Research agent (club order):
            # execute project-targeted decisions first, then the rest.
            decisions.sort(key=lambda d: 0 if d.agent_target == AgentTarget.PROJECT else 1)
            for decision in decisions:
                result.signals.append(decision.signal)
                result.decisions.append(decision)
                self._execute(decision, result)
            self._update_group_context(thread, decisions)

        # Mark seen only after the batch is handled, so a crash reprocesses.
        processed = [m.id for m in raw if m.group_id in self.allowlist]
        self.memory.mark_seen(processed)
        result.processed_ids = processed
        return result

    def _handle_joins(self, thread) -> None:
        """When someone joins a group, send them a contextual welcome summary
        of what the group has been discussing (agentic: group context on join)."""
        for m in thread.messages:
            if m.kind == MessageKind.SYSTEM and m.text.startswith("[join]"):
                ctx = self.memory.get_group_context(thread.group_id)
                if ctx:
                    welcome = (
                        f"Welcome! Here's what this group has been up to recently:\n{ctx}"
                    )
                    self.sender(thread.group_id, welcome)

    def _update_group_context(self, thread, decisions) -> None:
        """Keep a short rolling summary per group so outbound messages can be
        phrased in that group's context."""
        notable = [d.signal.summary for d in decisions
                   if d.signal.type not in (SignalType.NOISE, SignalType.QUESTION)]
        if notable:
            self.memory.update_group_context(thread.group_id, "; ".join(notable)[:200])

    def _execute(self, decision: RoutingDecision, result: IngestResult) -> None:
        signal = decision.signal

        # 1. ambiguous -> ask in-chat, do not act on a guess
        if signal.needs_clarification and signal.clarification_question:
            self.sender(signal.group_id, signal.clarification_question)
            result.clarifications.append(signal.clarification_question)

        # 2. write to the wiki (WA agent is a source of truth for chat info)
        if decision.wiki_method:
            self._write(decision.wiki_method, signal)

        # 3. trigger the owning agent (research / innovation / project)
        if decision.agent_target != AgentTarget.NONE:
            self.dispatcher.trigger(decision.agent_target, signal)

        # 4. echo notable items back to WhatsApp
        if decision.should_notify:
            self.memory.enqueue_notification(NotificationIntent(
                id=signal.id, source_agent="wa", audience=signal.group_id,
                payload=signal.summary, source_ids=signal.source_message_ids,
            ))

    def _write(self, method: str, signal: Signal) -> None:
        fn = getattr(self.memory, method)
        if method == "write_chat_digest":
            fn(signal.group_id, signal.summary, signal.source_message_ids)
        elif method in ("write_mom", "write_user_ping"):
            fn(signal.group_id, signal.summary, signal.source_message_ids)
        else:
            fn(signal)

    # -- outbound --------------------------------------------------------

    def flush_notifications(self, registry=None, summariser=None) -> int:
        """Drain the outbound queue. For each intent: resolve its (possibly
        logical) audience to concrete group JIDs via the GroupRegistry, then
        summarise it for EACH target group in that group's context, and send.

        - registry: GroupRegistry. If None, `intent.audience` is treated as a
          literal JID (back-compat, used by the mock demo).
        - summariser: (intent, group_ctx) -> str. Defaults to Kimi via
          llm_client.summarise_notification, with a plain-format fallback.
        Returns the number of messages sent."""
        from llm_client import summarise_notification

        def default_summary(intent, group):
            ctx = self.memory.get_group_context(group.jid) if group else ""
            name = group.name if group else ""
            return summarise_notification(intent.payload, intent.source_agent, name, ctx)

        summarise = summariser or default_summary
        pending = self.memory.poll_pending_notifications()
        sent = 0
        for intent in pending:
            targets = registry.resolve(intent.audience) if registry else [intent.audience]
            if not targets:
                # nothing resolved (no announce group configured) — leave it
                # queued rather than dropping it, and note it.
                continue
            for jid in targets:
                group = registry.get(jid) if registry else None
                self.sender(jid, summarise(intent, group))
                sent += 1
            self.memory.mark_notification_sent(intent.id)
        return sent
