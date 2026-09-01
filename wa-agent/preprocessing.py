"""
The inbound preprocessing pipeline: gowa webhook events -> a clean list of
Threads ready for the (expensive) classifier.

Stages, cheapest-and-most-disqualifying first:

  0. (receive & persist raw)   -- handled by the webhook layer, not here
  1. dedup + order             -- drop redelivered ids, sort by time
  2. group allowlist           -- only project + TLDR/announcement groups
  3. type routing / normalize  -- every kind -> a text stand-in (+ flags)
  4. cheap pre-filter          -- heuristic noise drop (protects LLM budget)
  5. thread assembly           -- group by reply-chain + time window

Everything here is plain, deterministic, and unit-testable. No LLM, no
network. The only injected dependency is a MediaProcessor.
"""
from __future__ import annotations

from datetime import datetime, timezone

from schemas import RawMessage, NormalizedMessage, MessageKind, Thread
from media import MediaProcessor, StubMediaProcessor


# Stage 4 config -------------------------------------------------------------

# Pure-noise messages we drop before spending any LLM tokens.
_NOISE_EXACT = {
    "ok", "okk", "okay", "k", "kk", "thanks", "thank you", "ty", "tysm",
    "done", "cool", "nice", "lol", "lmao", "haha", "+1", "yes", "no", "yep",
    "nope", "sure", "hmm", "great", "gg", "same", "bump", "gm", "gn",
}
_MIN_KEEP_WORDS = 3        # shorter than this AND not signal-bearing -> drop
_SIGNAL_HINTS = (
    "http://", "https://", "arxiv", "github.com", "?",       # links / questions
    "deadline", "event", "meet", "tomorrow", "today", "pm", "am",
    "idea", "propose", "paper", "update", "done", "blocked", "todo",
    "task", "pr ", "merge", "deploy", "bug", "fix",
)


# Stage 1 --------------------------------------------------------------------

def dedup_and_order(messages: list[RawMessage], seen_ids: set[str]) -> list[RawMessage]:
    """Drop ids already seen (gowa redelivers on reconnect) and duplicates
    within this batch; return sorted by timestamp. `seen_ids` is updated."""
    out: list[RawMessage] = []
    batch_ids: set[str] = set()
    for m in messages:
        if m.id in seen_ids or m.id in batch_ids:
            continue
        batch_ids.add(m.id)
        out.append(m)
    out.sort(key=lambda m: m.timestamp)
    return out


# Stage 2 --------------------------------------------------------------------

def filter_groups(messages: list[RawMessage], allowlist: set[str]) -> list[RawMessage]:
    """Keep only messages from allowlisted groups (project + TLDR/announce)."""
    return [m for m in messages if m.group_id in allowlist]


# Stage 3 --------------------------------------------------------------------

def normalize(msg: RawMessage, media: MediaProcessor) -> NormalizedMessage | None:
    """Reduce any message kind to a text stand-in. Returns None for kinds we
    drop wholesale (stickers/gifs, most system messages)."""
    ts = datetime.fromtimestamp(msg.timestamp, tz=timezone.utc)

    def make(text: str, **flags) -> NormalizedMessage:
        return NormalizedMessage(
            id=msg.id, group_id=msg.group_id, sender=msg.sender, ts=ts,
            kind=msg.kind, text=text, quoted_id=msg.quoted_id, source=msg, **flags,
        )

    if msg.kind == MessageKind.STICKER:
        return None
    if msg.kind == MessageKind.SYSTEM:
        return None

    if msg.kind == MessageKind.TEXT or msg.kind == MessageKind.LINK:
        text = (msg.text or "").strip()
        return make(text) if text else None

    if msg.kind == MessageKind.IMAGE:
        return make(media.image_to_text(msg.media_ref, msg.text), media_unresolved=not msg.text)

    if msg.kind == MessageKind.DOCUMENT:
        return make(media.document_to_text(msg.media_ref, msg.filename), media_unresolved=True)

    if msg.kind == MessageKind.VOICE:
        transcript = media.transcribe_voice(msg.media_ref)
        if transcript:
            return make(transcript)
        dur = f"{msg.duration_s}s" if msg.duration_s else "unknown length"
        important = _voice_looks_important(msg)
        return make(
            f"[voice note from {msg.sender}, {dur}]",
            needs_human_tldr=important, media_unresolved=True,
        )

    if msg.kind == MessageKind.POLL:
        return make(f"[poll: {msg.text}]")

    text = (msg.text or "").strip()
    return make(text) if text else None


def _voice_looks_important(msg: RawMessage) -> bool:
    """Cheap heuristic for 'worth asking a human to summarise': it's part of
    an active exchange (a reply, or drew replies)."""
    return msg.quoted_id is not None or msg.reply_count >= 1


# Stage 4 --------------------------------------------------------------------

def is_noise(m: NormalizedMessage) -> bool:
    """True if the message should be dropped before classification."""
    if m.needs_human_tldr or m.media_unresolved:
        return False  # unresolved media is a decision to keep, not noise
    t = m.text.strip().lower()
    if not t:
        return True
    if t in _NOISE_EXACT:
        return True
    if any(h in t for h in _SIGNAL_HINTS):
        return False
    if len(t.split()) < _MIN_KEEP_WORDS:
        return True
    return False


def prefilter(messages: list[NormalizedMessage]) -> list[NormalizedMessage]:
    return [m for m in messages if not is_noise(m)]


# Stage 5 --------------------------------------------------------------------

def assemble_threads(
    messages: list[NormalizedMessage],
    gap_seconds: int = 900,      # 15 min quiet gap starts a new thread
) -> list[Thread]:
    """Group surviving messages into threads per group, splitting on a time
    gap. Reply chains keep messages together even across the gap."""
    by_group: dict[str, list[NormalizedMessage]] = {}
    for m in messages:
        by_group.setdefault(m.group_id, []).append(m)

    threads: list[Thread] = []
    for group_id, msgs in by_group.items():
        msgs.sort(key=lambda m: m.ts)
        ids_in_current: set[str] = set()
        current = Thread(group_id=group_id)
        last_ts = None
        for m in msgs:
            is_reply_into_current = m.quoted_id in ids_in_current
            gap_ok = last_ts is not None and (m.ts - last_ts).total_seconds() <= gap_seconds
            if current.messages and not gap_ok and not is_reply_into_current:
                threads.append(current)
                current = Thread(group_id=group_id)
                ids_in_current = set()
            current.messages.append(m)
            ids_in_current.add(m.id)
            last_ts = m.ts
        if current.messages:
            threads.append(current)
    return threads


# Orchestration --------------------------------------------------------------

def preprocess(
    raw: list[RawMessage],
    allowlist: set[str],
    seen_ids: set[str],
    media: MediaProcessor | None = None,
) -> list[Thread]:
    """Run stages 1-5 and return classifier-ready threads. Does NOT mutate
    persistent seen-state — the caller marks ids seen after a successful
    run, so a crash mid-batch reprocesses rather than silently drops."""
    media = media or StubMediaProcessor()
    ordered = dedup_and_order(raw, seen_ids)
    in_scope = filter_groups(ordered, allowlist)
    normalized = [n for n in (normalize(m, media) for m in in_scope) if n is not None]
    kept = prefilter(normalized)
    return assemble_threads(kept)
