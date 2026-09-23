"""Timeline line grammar (Wiki Format, thread page): one line per message,
`- <yyyy-mm-dd HH:MM> — <who>: <text> [src:: id1, id2] [reply_to:: id]`.
Written and read back here only; the text is always a single line.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

_SRC_RE = re.compile(r"\[src::\s*([^\]]*)\]")


def format_timeline_line(
    when: datetime, who: str, text: str, src_ids: Sequence[str], reply_to: str | None = None
) -> str:
    flat = " ".join(part.strip() for part in text.splitlines() if part.strip())
    reply = f" [reply_to:: {reply_to}]" if reply_to else ""
    return f"- {when.strftime('%Y-%m-%d %H:%M')} — {who}: {flat} [src:: {', '.join(src_ids)}]{reply}"


def timeline_lines(fenced_body: str) -> list[str]:
    return [line.strip() for line in fenced_body.splitlines() if line.strip().startswith("- ")]


def timeline_src_ids(line: str) -> list[str]:
    """The message ids a Timeline line carries (more than one when
    same-sender messages were folded together)."""
    match = _SRC_RE.search(line)
    return [s.strip() for s in match.group(1).split(",") if s.strip()] if match else []
