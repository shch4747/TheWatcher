"""Read-only view of an experiment: which thread did each message land in?

Standard library only. Reads the experiment's files directly:
  * watcher.db  -> `channels` and `messages_buffer` (the replayed chat)
  * vault/      -> the thread pages the agent wrote (frontmatter + body)
A message belongs to a thread if its id is in the thread's `message_ids`
frontmatter or appears as a `[src:: id]` tag in the page body. Anything in
no thread is shown as chatter/unassigned.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from testbench.groups import slug

_SRC = re.compile(r"\[src::\s*([^\]]+)\]")
_COMMENT = re.compile(r"<!--.*?-->", re.S)


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    meta: dict = {}
    key: str | None = None
    for line in m.group(1).splitlines():
        if key and re.match(r"^\s+-\s+", line):  # block-style list item
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(_unquote(re.sub(r"^\s+-\s+", "", line)))
            continue
        k, sep, v = line.partition(":")
        if not sep:
            continue
        key, v = k.strip(), v.strip()
        if v.startswith("["):
            try:
                meta[key] = [str(x) for x in json.loads(v)]
            except ValueError:
                meta[key] = [_unquote(x) for x in v.strip("[]").split(",") if x.strip()]
        elif v == "":
            meta[key] = []  # maybe a block list follows
        else:
            meta[key] = _unquote(v)
    return meta, m.group(2)


def _summary(body: str) -> str:
    m = re.search(r"^##\s*Summary\s*\n(.*?)(?=^##\s|\Z)", body, re.S | re.M)
    text = _COMMENT.sub("", m.group(1) if m else "").strip()
    return " ".join(text.split())[:600]


def load_threads(vault: Path) -> list[dict]:
    out = []
    base = Path(vault) / "channels"
    if not base.is_dir():
        return out
    for p in sorted(base.rglob("*.md")):
        meta, body = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        if meta.get("type") != "thread":
            continue
        ids = list(meta.get("message_ids") or [])
        ids += [i.strip() for tag in _SRC.findall(body) for i in re.split(r"[,\s]+", tag) if i.strip()]
        out.append(
            {
                "slug": p.stem,
                "path": p.relative_to(vault).as_posix(),
                "title": meta.get("title") or p.stem,
                "state": meta.get("state") or "active",
                "summary": _summary(body),
                "participants": meta.get("participants") or [],
                "opened_at": meta.get("opened_at"),
                "last_message_at": meta.get("last_message_at"),
                "archived": "/archive/" in p.as_posix(),
                "ids": sorted(set(ids)),
            }
        )
    return out


def _db(exp_dir: Path) -> sqlite3.Connection | None:
    f = Path(exp_dir) / "watcher.db"
    if not f.is_file():
        return None
    con = sqlite3.connect(f"file:{f}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def list_channels(exp_dir: Path) -> list[dict]:
    con = _db(exp_dir)
    if con is None:
        return []
    try:
        counts = {
            r["channel"]: r["n"]
            for r in con.execute("SELECT channel, COUNT(*) n FROM messages_buffer GROUP BY channel")
        }
        return [
            {
                "jid": r["jid"],
                "title": r["title"] or r["jid"],
                "kind": r["kind"],
                "messages": counts.get(r["jid"], 0),
            }
            for r in con.execute("SELECT jid, title, kind FROM channels WHERE kind != 'unset' ORDER BY id")
        ]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def build_stream(exp_dir: Path, jid: str) -> dict:
    exp_dir = Path(exp_dir)
    con = _db(exp_dir)
    if con is None:
        return {"channel": None, "threads": [], "messages": []}
    try:
        ch = con.execute("SELECT jid, title, kind FROM channels WHERE jid=?", (jid,)).fetchone()
        rows = con.execute(
            "SELECT message_id, payload FROM messages_buffer WHERE channel=? ORDER BY id", (jid,)
        ).fetchall()
    except sqlite3.Error:
        return {"channel": None, "threads": [], "messages": []}
    finally:
        con.close()
    title = (ch["title"] if ch else None) or jid
    prefixes = (f"channels/{slug(title)}/", f"channels/archive/{slug(title)}/")
    threads = [t for t in load_threads(exp_dir / "vault") if t["path"].startswith(prefixes)]
    owner = {mid: t["slug"] for t in threads for mid in t["ids"]}
    messages = []
    for r in rows:
        p = json.loads(r["payload"]).get("payload", {})
        messages.append(
            {
                "id": r["message_id"],
                "t": p.get("timestamp"),
                "who": p.get("from_name") or "(no name)",
                "text": p.get("body", ""),
                "media": next(
                    (k for k in ("image", "video", "audio", "sticker", "document") if p.get(k)), None
                ),
                "thread": owner.get(r["message_id"]),
            }
        )
    counts: dict[str, int] = {}
    for m in messages:
        if m["thread"]:
            counts[m["thread"]] = counts.get(m["thread"], 0) + 1
    for t in threads:
        t["messages"] = counts.get(t["slug"], 0)
        del t["ids"]
    threads.sort(key=lambda t: t.get("opened_at") or "")
    return {
        "channel": {"jid": jid, "title": title, "kind": ch["kind"] if ch else None},
        "threads": threads,
        "messages": messages,
        "unassigned": sum(1 for m in messages if not m["thread"]),
    }
