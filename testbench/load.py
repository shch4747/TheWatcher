"""Put parsed messages into the experiment's `messages_buffer`, exactly as the
Gateway's history import (`/setup` backfill) would: gowa-shaped JSON payloads,
`message.backfill` events, arrival order = chronological order, `received_at` =
the original send time (the agent's same-sender merge window and batch timing
read `received_at`, so it must not be "now").
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter

from shared.db import MessageBuffer, get_session

from testbench.dataset import GroupData
from testbench.parse_export import DEFAULT_TZ  # noqa: F401  (re-exported for convenience)


def _canon(text: str) -> str:
    return " ".join(text.split()).casefold()


async def load_group_messages(gd: GroupData) -> int:
    seen: Counter[tuple] = Counter()
    rows: list[MessageBuffer] = []
    for m in gd.messages:
        iso = m.sent_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        k = (iso, m.person.key, _canon(m.text))
        seen[k] += 1
        mid = "tb-" + hashlib.sha1(f"{gd.group.jid}|{k}|{seen[k]}".encode()).hexdigest()[:16]
        payload: dict = {
            "id": mid,
            "chat_id": gd.group.jid,
            "from": m.person.wa_identity,
            "body": m.text,
            "timestamp": iso,
        }
        if m.person.display:
            payload["from_name"] = m.person.display
        if m.media_kind:
            payload[m.media_kind] = True
        rows.append(
            MessageBuffer(
                message_id=mid,
                channel=gd.group.jid,
                event_type="message.backfill",
                payload=json.dumps({"event": "message.backfill", "payload": payload}, ensure_ascii=False),
                received_at=m.sent_at,
                processed=False,
            )
        )
    async with get_session() as session:
        session.add_all(rows)
        await session.commit()
    return len(rows)
