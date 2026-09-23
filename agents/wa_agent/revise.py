"""Thread revision (ADR-0012 step 5): new messages in, a clean, ready-to-
write Thread out - for a Thread being started and one being continued
alike.

One call does the whole job: the structured thread-update model call
(`assign.update_thread`), Item grounding, and every guard on what the
model said. The title and summary guards used to live in `run_batch` and
were applied differently on its two branches; they live here now, so
whatever a model returns, neither branch can write a bad title.

The title policy, in order: the model's title, else the title the Thread
already had (or the one assignment gave a new Thread), else the first
message's own words - each candidate cleaned, and skipped if it is empty
or reads as the model commenting on its task instead of doing it.

Internal to `agents/wa_agent`; `interface.py` re-exports the public names.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from shared.models.interface import TextModelClient
from shared.wiki.interface import StoredThread, format_timeline_line

from agents.wa_agent.assign import NewThread, SenderNames, update_thread
from agents.wa_agent.messages import BufferedMessage

TITLE_MAX_WORDS = 5
TITLE_MAX_CHARS = 60  # safety net for pathologically long "words" (no spaces)
SUMMARY_MAX_CHARS = 800
FALLBACK_TITLE = "Untitled thread"


@dataclass
class ThreadRevision:
    title: str
    summary: str
    items_body: str
    timeline_lines: list[str]
    participants: list[str]
    message_ids: list[str]
    opened_at: datetime
    last_message_at: datetime


# --------------------------------------------------------------------------
# guards
# --------------------------------------------------------------------------

_META_COMMENTARY_RE = re.compile(
    r"\b(transcript|inert data|third-party|third party|no substantive content|"
    r"no action required|not sure what|as an ai|i notice|understood[,.]?$|"
    r"as requested|per (the|your) instructions)\b",
    re.IGNORECASE,
)
_TITLE_LEADING_MARKER_RE = re.compile(r"^[\-*•]+\s*|^\d+[.)]\s*")
# A leading label like "Title:", "**Message summary:**", "Summary -" -
# real observed output was a whole "**Message summary:** An incoming
# message..." sentence, not a title.
_TITLE_LEADING_LABEL_RE = re.compile(r"^[*_]{0,2}[A-Za-z][A-Za-z \-]{0,30}[*_]{0,2}:\*{0,2}\s*-?\s*")
_CODE_FENCE_RE = re.compile(r"^```\w*\s*$", re.MULTILINE)


def _is_meta_commentary(text: str) -> bool:
    """Even told not to, a Worker model can describe its own task instead
    of doing it - "Understood, the transcript has been received and
    treated as inert data" is a real observed title. Well-formed prose,
    so only a content check catches it."""
    return bool(_META_COMMENTARY_RE.search(text))


def clean_title(raw: str) -> str:
    """One candidate title -> a plain single line of at most
    TITLE_MAX_WORDS words, or "" if nothing usable is left. Strips list
    markers ("- ", "1. "), label preambles ("**Summary:**"), code fences,
    markdown emphasis and wrapping quotes."""
    text = _CODE_FENCE_RE.sub("", raw or "").strip()
    text = _TITLE_LEADING_MARKER_RE.sub("", text)
    text = _TITLE_LEADING_LABEL_RE.sub("", text)
    text = re.sub(r"[`*_]", "", text).strip().strip("\"'")
    line = re.sub(r"\s+", " ", text.splitlines()[0] if text.strip() else "").strip()
    if not line or _is_meta_commentary(line):
        return ""
    words = line.split(" ")
    if len(words) > TITLE_MAX_WORDS:
        line = " ".join(words[:TITLE_MAX_WORDS]).rstrip(",.;:- ")
    if len(line) > TITLE_MAX_CHARS:
        head = line[:TITLE_MAX_CHARS]
        line = (head.rsplit(" ", 1)[0] if " " in head else head).rstrip(",.;:- ")
    return line


def choose_title(*candidates: str) -> str:
    """The first candidate that survives `clean_title`."""
    for candidate in candidates:
        cleaned = clean_title(candidate)
        if cleaned:
            return cleaned
    return FALLBACK_TITLE


def clean_summary(raw: str) -> str:
    """One paragraph, capped at SUMMARY_MAX_CHARS on a sentence boundary;
    "" for meta-commentary (worse than an empty Summary)."""
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if _is_meta_commentary(text):
        return ""
    if len(text) > SUMMARY_MAX_CHARS:
        head = text[:SUMMARY_MAX_CHARS]
        text = (head.rsplit(". ", 1)[0] + "." if ". " in head else head).rstrip()
    return text


# --------------------------------------------------------------------------
# page lines
# --------------------------------------------------------------------------


def timeline_line(message: BufferedMessage, names: SenderNames) -> str:
    """Who is the member wikilink when the sender is linked, else their
    display name - never a bare number when gowa gave us a name."""
    return format_timeline_line(
        message.when, names.wiki_name(message), message.text, message.src_ids, message.replied_to_id
    )


def participants(messages: list[BufferedMessage], names: SenderNames) -> list[str]:
    return sorted({names.wiki_name(m) for m in messages})


# --------------------------------------------------------------------------
# the one call
# --------------------------------------------------------------------------


async def revise_thread(
    model: TextModelClient,
    messages: list[BufferedMessage],
    names: SenderNames,
    *,
    current: StoredThread | None = None,
    minted: NewThread | None = None,
) -> ThreadRevision:
    """New messages for a Thread -> its revised title, Summary and Items,
    and the Timeline lines, participants and ids to add. Pass `current`
    for an existing Thread, `minted` (what assignment declared) for a new
    one. One model call."""
    if current is not None:
        seed_title, seed_summary, items_body = current.title, current.summary, current.items_body
        known_ids = set(current.message_ids)
    else:
        seed_title = minted.title if minted else ""
        seed_summary = minted.description.strip() if minted else ""
        items_body, known_ids = "", set()

    batch_ids = [src_id for m in messages for src_id in m.src_ids]
    update, items_text = await update_thread(
        model, seed_title, seed_summary, items_body, messages, names, set(batch_ids) | known_ids
    )
    return ThreadRevision(
        title=choose_title(update.title, seed_title, messages[0].text),
        summary=clean_summary(update.summary) or seed_summary,
        items_body=items_text,
        timeline_lines=[timeline_line(m, names) for m in messages],
        participants=participants(messages, names),
        message_ids=batch_ids,
        opened_at=messages[0].when,
        last_message_at=messages[-1].when,
    )
