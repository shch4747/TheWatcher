"""
Core data structures for the WhatsApp (WA) Agent.

Plain, serializable dataclasses on purpose — same discipline as the
Innovation Agent's schemas: they should be writable straight to Lapis (or
any memory backend) as JSON/frontmatter without a translation layer.

Flow these types travel through:

    gowa webhook  -> RawMessage
                  -> (preprocessing) -> NormalizedMessage
                  -> (thread assembly) -> Thread
                  -> (classify)  -> Signal
                  -> (router)    -> RoutingDecision
                  -> (writeback / agent trigger / outbound notification)
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# --------------------------------------------------------------------------
# Inbound: what WhatsApp gives us
# --------------------------------------------------------------------------

class MessageKind(str, Enum):
    """The normalized kind of a WhatsApp message, collapsed from gowa's
    richer set into what our pipeline actually treats differently."""
    TEXT = "text"
    IMAGE = "image"
    VOICE = "voice"          # audio / ptt voice note
    DOCUMENT = "document"
    LINK = "link"
    POLL = "poll"
    STICKER = "sticker"      # includes animated stickers / gifs — dropped
    SYSTEM = "system"        # joins/leaves/renames — mostly dropped
    OTHER = "other"


@dataclass
class RawMessage:
    """A single message as it comes off the gowa webhook, lightly shaped.

    The gowa/whatsmeow payload is richer than this; the ingestion adapter
    is responsible for mapping the real payload onto these fields. Keeping
    this thin means the rest of the pipeline never sees gowa specifics.
    """
    id: str                              # WhatsApp message id (dedup key)
    group_id: str                        # chat/group JID
    sender: str                          # sender JID or display name
    timestamp: float                     # epoch seconds
    kind: MessageKind = MessageKind.TEXT
    text: str = ""                       # body or caption
    quoted_id: Optional[str] = None      # id of the message this replies to
    media_ref: Optional[str] = None      # opaque handle to fetch media
    duration_s: Optional[int] = None     # for voice notes
    filename: Optional[str] = None       # for documents
    reply_count: int = 0                 # replies observed to this message
    raw: Optional[dict] = None           # original payload, kept for debugging


@dataclass
class NormalizedMessage:
    """A RawMessage after type-routing: every kind is reduced to a text
    stand-in the classifier can read, plus flags the classifier needs."""
    id: str
    group_id: str
    sender: str
    ts: datetime
    kind: MessageKind
    text: str                            # the classifier-readable stand-in
    quoted_id: Optional[str] = None
    needs_human_tldr: bool = False       # e.g. an important voice note we won't transcribe
    media_unresolved: bool = False       # media we couldn't turn into text
    source: Optional[RawMessage] = None


@dataclass
class Thread:
    """A coherent chunk of conversation handed to the classifier together,
    grouped by reply-chain and time-window."""
    group_id: str
    messages: list[NormalizedMessage] = field(default_factory=list)

    def as_transcript(self) -> str:
        lines = []
        for m in self.messages:
            stamp = m.ts.strftime("%H:%M")
            lines.append(f"[{stamp}] {m.sender}: {m.text}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# After classification: typed signals and where they route
# --------------------------------------------------------------------------

class SignalType(str, Enum):
    """What a message (or thread) turned out to be. Drives routing to the
    other agents and to the wiki."""
    RESEARCH_PAPER = "research_paper"    # -> Research Agent
    PROJECT_IDEA = "project_idea"        # -> Innovation Agent
    EVENT_IDEA = "event_idea"            # -> Innovation Agent
    PROJECT_UPDATE = "project_update"    # -> Project Agent
    EVENT_INFO = "event_info"            # -> wiki events + reminders
    TASK = "task"                        # -> wiki tasks (+ Project Agent)
    DECISION = "decision"                # -> wiki digest
    ANNOUNCEMENT = "announcement"        # -> wiki digest
    QUESTION = "question"                # -> wiki digest (maybe clarify)
    FEEDBACK = "feedback"                # -> Innovation Agent (event/project feedback)
    MOM = "mom"                          # -> wiki (minutes of meeting)
    USER_PING = "user_ping"              # -> wiki (manual lapis update via bot)
    NOISE = "noise"                      # dropped, never routed


class AgentTarget(str, Enum):
    RESEARCH = "research"
    INNOVATION = "innovation"
    PROJECT = "project"
    NONE = "none"


# Which agent each signal type feeds. The single source of truth for the
# "research papers -> research agent, ideas -> innovation, updates ->
# project" routing rule.
SIGNAL_TO_AGENT: dict[SignalType, AgentTarget] = {
    SignalType.RESEARCH_PAPER: AgentTarget.RESEARCH,
    SignalType.PROJECT_IDEA: AgentTarget.INNOVATION,
    SignalType.EVENT_IDEA: AgentTarget.INNOVATION,
    SignalType.FEEDBACK: AgentTarget.INNOVATION,       # event/project feedback -> Innovation
    SignalType.PROJECT_UPDATE: AgentTarget.PROJECT,
    SignalType.TASK: AgentTarget.PROJECT,
    SignalType.EVENT_INFO: AgentTarget.NONE,
    SignalType.DECISION: AgentTarget.NONE,
    SignalType.ANNOUNCEMENT: AgentTarget.NONE,
    SignalType.QUESTION: AgentTarget.NONE,
    SignalType.MOM: AgentTarget.NONE,                  # wiki-only
    SignalType.USER_PING: AgentTarget.NONE,            # wiki-only (manual update)
    SignalType.NOISE: AgentTarget.NONE,
}


@dataclass
class Signal:
    """A classified unit of meaning extracted from a thread."""
    id: str
    type: SignalType
    summary: str                          # one-line, human-readable
    source_message_ids: list[str]
    group_id: str
    project_ref: Optional[str] = None     # project slug/id, if it names one
    confidence: float = 0.0
    needs_clarification: bool = False
    clarification_question: Optional[str] = None
    payload: dict = field(default_factory=dict)   # type-specific extras (e.g. paper url)


@dataclass
class RoutingDecision:
    """The declarative result of routing one Signal — what to write to the
    wiki, which agent to poke, and whether it's worth announcing. The
    pipeline executes this; the router only decides it (keeps it testable)."""
    signal: Signal
    wiki_method: Optional[str]            # MemoryInterface method to call, or None
    agent_target: AgentTarget
    should_notify: bool = False


# --------------------------------------------------------------------------
# Outbound: the notification gateway
# --------------------------------------------------------------------------

class Urgency(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


@dataclass
class NotificationIntent:
    """What another agent enqueues when it has something worth sharing on
    WhatsApp. The WA agent drains these, summarises each into a message,
    batches, and sends via gowa. Producers never touch gowa directly."""
    id: str
    source_agent: str                     # "research" | "innovation" | "project" | ...
    audience: str                         # group JID or logical group name
    payload: str                          # the raw thing to summarise
    urgency: Urgency = Urgency.NORMAL
    source_ids: list[str] = field(default_factory=list)
    created_at: Optional[datetime] = None
    sent: bool = False
