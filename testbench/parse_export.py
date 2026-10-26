"""Parse a WhatsApp "Export chat" .txt (Android *or* iPhone) into messages.

Pure standard library on purpose: this module (and everything it needs for
`check`) must import without the Watcher's `shared/` packages.

Handles
-------
* Android   ``12/03/2025, 14:05 - Aman: text``        (24h or 12h am/pm)
* iPhone    ``[12/03/2025, 14:05:33] Aman: text``     (24h or 12h AM/PM)
* invisible direction marks (U+200E etc.) and narrow no-break spaces
  (U+202F, put before AM/PM by newer iOS/Android)
* multi-line messages (continuation lines have no timestamp)
* system lines (joined/left/added/encryption notice/subject changes ...) -> dropped
* deleted messages -> dropped; "<This message was edited>" suffix -> stripped
* media placeholders ("<Media omitted>", "image omitted", "<attached: x.jpg>",
  "x.jpg (file attached)") -> recorded as media, caption kept as text
* day-first vs month-first dates, auto-detected per file (see `_detect_order`)

Timestamps in an export carry no timezone; they are interpreted in `tz`
(default Asia/Kolkata) and returned as aware UTC datetimes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Kolkata"

_CLEAN = {ord(c): None for c in "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff"}
_CLEAN.update({0x202F: " ", 0x00A0: " "})

_HEADER = re.compile(
    r"^\[?(?P<d1>\d{1,4})[/.\-](?P<d2>\d{1,2})[/.\-](?P<d3>\d{1,4}),?\s+"
    r"(?P<h>\d{1,2})[:.](?P<m>\d{2})(?:[:.](?P<s>\d{2}))?\s*"
    r"(?P<ampm>[AaPp]\.?\s?[Mm]\.?)?"
    r"\s*(?:\]\s*|[-\u2013\u2014]\s+)(?P<rest>.*)$"
)

# Applied to the *sender part* of "<sender>: <body>" and to header lines that
# have no sender at all. A line whose sender part looks like one of these is a
# group event, e.g.  Aman changed the subject from "A: B" to "C".
_SYSTEM_SENDER = re.compile(
    r"\b(added|removed|joined|left|created (this )?group|changed (the |this |their )"
    r"|changed to|is now an admin|no longer an admin|you're now an admin|"
    r"turned (on|off)|pinned a message|security code|deleted this group|"
    r"requested to join|was added|were added|invite link)\b",
    re.IGNORECASE,
)

# iOS prints group events as "<sender>: <U+200E><event>" (e.g. `Aman: created group "X"`,
# `Aman: added Karan`, and the encryption notice "from" the group name). Real messages never
# start with the direction mark, except media placeholders, which are checked first.
_SYSTEM_BODY_IOS = re.compile(
    r"^(created (this )?group|added |removed |left$|joined |changed |you're now an admin|"
    r"you were added|you joined|turned (on|off)|pinned a message|.*\bwas added\b|.*invite link)",
    re.IGNORECASE,
)
_ENCRYPTION = re.compile(r"end-to-end encrypted", re.IGNORECASE)

_DELETED = {"this message was deleted", "you deleted this message", "this message was deleted."}
_EDITED = re.compile(r"\s*<This message was edited>\s*$", re.IGNORECASE)
_MEDIA = re.compile(
    r"^<?(?P<k>media|image|video|audio|sticker|gif|document|contact card|voice message|video note)"
    r"\s+omitted>?$",
    re.IGNORECASE,
)
_ATTACHED = re.compile(r"^<attached:\s*(?P<f>[^>]+)>$|^(?P<f2>.+?)\s*\(file attached\)$", re.IGNORECASE)

_MEDIA_WORDS = {
    "media": "image",
    "image": "image",
    "video": "video",
    "audio": "audio",
    "sticker": "sticker",
    "gif": "video",
    "document": "document",
    "contact card": "document",
    "voice message": "audio",
    "video note": "video_note",
}
_EXT_KIND = {
    "jpg": "image",
    "jpeg": "image",
    "png": "image",
    "heic": "image",
    "webp": "image",
    "mp4": "video",
    "mov": "video",
    "3gp": "video",
    "mkv": "video",
    "opus": "audio",
    "m4a": "audio",
    "mp3": "audio",
    "ogg": "audio",
    "aac": "audio",
}


@dataclass
class ParsedMessage:
    sender: str  # as printed in the export (the exporter's view of the name)
    sent_at: datetime  # aware, UTC
    text: str  # may be "" for media-only
    media_kind: str | None
    has_seconds: bool
    line_no: int
    source: str


@dataclass
class ParseReport:
    source: str
    lines: int = 0
    messages: int = 0
    system_dropped: int = 0
    deleted_dropped: int = 0
    media_only: int = 0
    date_order: str = "dmy"
    date_order_basis: str = "default"  # unambiguous | chronology | default | forced
    first: datetime | None = None
    last: datetime | None = None
    warnings: list[str] = field(default_factory=list)


def _kind_from_name(name: str) -> str:
    name = name.strip()
    if name.upper().startswith("STK-"):
        return "sticker"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _EXT_KIND.get(ext, "document")


def _year(y: int) -> int:
    return y + 2000 if y < 100 else y


def _detect_order(triples: list[tuple[int, int, int]], default_dayfirst: bool) -> tuple[bool, str, list[str]]:
    """Is the first number the day? Decide from the file itself.

    1. unambiguous: some first number is > 12 (so it must be a day), or some
       second number is > 12 (so it must be a day, i.e. month-first).
    2. chronology: an export is chronological, so choose the order under which
       the dates run forwards with fewer steps backwards in time.
    3. default: genuinely ambiguous (e.g. everything within one month with
       days <= 12): fall back to `default_dayfirst` and say so.
    """
    warnings: list[str] = []
    first_big = any(a > 12 for a, _, _ in triples)
    second_big = any(b > 12 for _, b, _ in triples)
    if first_big and not second_big:
        return True, "unambiguous", warnings
    if second_big and not first_big:
        return False, "unambiguous", warnings
    if first_big and second_big:
        warnings.append("dates look inconsistent (day>12 in both positions); using default order")
        return default_dayfirst, "default", warnings

    def backsteps(first_is_day: bool) -> int:
        prev: date | None = None
        n = 0
        for a, b, y in triples:
            d, m = (a, b) if first_is_day else (b, a)
            try:
                cur = date(_year(y), m, d)
            except ValueError:
                n += 1000
                continue
            if prev is not None and cur < prev:
                n += 1
            prev = cur
        return n

    dmy, mdy = backsteps(True), backsteps(False)
    if dmy != mdy:
        return dmy < mdy, "chronology", warnings
    warnings.append(
        "date order is ambiguous in this file (all numbers <= 12 and both orders look chronological); "
        f"assuming {'day-first' if default_dayfirst else 'month-first'} - "
        "set dayfirst in exports.csv to override"
    )
    return default_dayfirst, "default", warnings


def _split_sender(rest: str) -> tuple[str, str] | None:
    i = rest.find(": ")
    if 0 < i <= 80:
        return rest[:i], rest[i + 2 :]
    if rest.endswith(":") and 0 < len(rest) <= 81:
        return rest[:-1], ""
    return None


def parse_export(
    text: str,
    *,
    tz: str = DEFAULT_TZ,
    dayfirst: bool | None = None,
    default_dayfirst: bool = True,
    source: str = "",
) -> tuple[list[ParsedMessage], ParseReport]:
    """Parse one export. `dayfirst=None` means auto-detect."""
    zone = ZoneInfo(tz)
    raw_lines = text.splitlines()
    lines = [ln.translate(_CLEAN).rstrip("\r\n") for ln in raw_lines]
    report = ParseReport(source=source, lines=len(lines))

    triples: list[tuple[int, int, int]] = []
    for ln in lines:
        m = _HEADER.match(ln)
        if m and len(m["d1"]) <= 2:
            triples.append((int(m["d1"]), int(m["d2"]), int(m["d3"])))

    if dayfirst is None:
        dayfirst, basis, warns = (
            _detect_order(triples, default_dayfirst) if triples else (default_dayfirst, "default", [])
        )
        report.warnings += warns
    else:
        basis = "forced"
    report.date_order, report.date_order_basis = ("dmy" if dayfirst else "mdy"), basis

    def to_dt(m: re.Match[str]) -> datetime | None:
        d1, d2, d3 = int(m["d1"]), int(m["d2"]), int(m["d3"])
        if len(m["d1"]) == 4:  # ISO-like yyyy-mm-dd
            y, mo, d = d1, d2, d3
        else:
            y = _year(d3)
            d, mo = (d1, d2) if dayfirst else (d2, d1)
        h = int(m["h"])
        ap = (m["ampm"] or "").lower().replace(".", "").replace(" ", "")
        if ap:
            h = h % 12 + (12 if ap == "pm" else 0)
        try:
            local = datetime(y, mo, d, h, int(m["m"]), int(m["s"] or 0), tzinfo=zone)
        except ValueError:
            return None
        return local.astimezone(UTC)

    out: list[ParsedMessage] = []
    cur: dict | None = None

    def flush() -> None:
        nonlocal cur
        if cur is None:
            return
        body = _EDITED.sub("", "\n".join(cur["lines"]).strip()).strip()
        sender, sent_at = cur["sender"], cur["dt"]
        meta = (cur["has_seconds"], cur["line_no"])
        cur = None
        if body.lower() in _DELETED:
            report.deleted_dropped += 1
            return
        media: str | None = None
        head, _, tail = body.partition("\n")
        mm = _MEDIA.match(head.strip())
        if mm:
            media, body = _MEDIA_WORDS[mm["k"].lower()], tail.strip()
        else:
            am = _ATTACHED.match(head.strip())
            if am:
                media, body = _kind_from_name(am["f"] or am["f2"]), tail.strip()
        if media and not body:
            report.media_only += 1
        out.append(ParsedMessage(sender, sent_at, body, media, meta[0], meta[1], source))

    for no, ln in enumerate(lines, 1):
        m = _HEADER.match(ln)
        dt = to_dt(m) if m else None
        if m is not None and dt is not None:
            flush()
            parts = _split_sender(m["rest"])
            if (
                parts is None
                or _SYSTEM_SENDER.search(parts[0])
                or '"' in parts[0]
                or len(parts[0].split()) > 6
            ):
                report.system_dropped += 1
                continue
            body0 = parts[1].strip()
            is_media = bool(_MEDIA.match(body0) or _ATTACHED.match(body0))
            ios_event = (
                ": \u200e" in raw_lines[no - 1] and not is_media and bool(_SYSTEM_BODY_IOS.match(body0))
            )
            if ios_event or _ENCRYPTION.search(body0.split("\n")[0]):
                report.system_dropped += 1
                continue
            sender = parts[0].lstrip("~ ").strip()
            cur = {
                "sender": sender,
                "dt": dt,
                "lines": [parts[1]],
                "has_seconds": m["s"] is not None,
                "line_no": no,
            }
        elif cur is not None:
            cur["lines"].append(ln)
    flush()

    report.messages = len(out)
    if out:
        report.first, report.last = min(p.sent_at for p in out), max(p.sent_at for p in out)
    elif lines:
        report.warnings.append("no messages found - is this a WhatsApp 'Export chat' text file?")
    return out, report


def parse_export_file(path: Path, **kwargs) -> tuple[list[ParsedMessage], ParseReport]:
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    return parse_export(text, source=Path(path).name, **kwargs)
