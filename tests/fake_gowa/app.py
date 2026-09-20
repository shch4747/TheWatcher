"""Fake gowa: serves the recorded OpenAPI shapes Phase 0-2 need
(Spec, Testing Decisions, Seam 1)."""
from __future__ import annotations

import uuid

from fastapi import FastAPI, Request

app = FastAPI(title="fake-gowa")
app.state.sent_messages: list[dict] = []
app.state.reactions: list[dict] = []
app.state.chat_history: dict[str, list[dict]] = {}


@app.post("/send/message")
async def send_message(request: Request) -> dict:
    body = await request.json()
    message_id = f"fake-{uuid.uuid4().hex[:12]}"
    app.state.sent_messages.append({**body, "message_id": message_id})
    return {"results": {"message_id": message_id}}


@app.post("/message/{message_id}/reaction")
async def react(message_id: str, request: Request) -> dict:
    body = await request.json()
    app.state.reactions.append({"message_id": message_id, **body})
    return {"results": {"status": "ok"}}


@app.get("/chat/{jid}/messages")
async def chat_messages(jid: str, limit: int = 100, before: str | None = None) -> dict:
    history = app.state.chat_history.get(jid, [])
    return {"results": history[-limit:]}


@app.get("/app/devices")
async def devices() -> dict:
    return {"results": [{"device": "fake", "status": "connected"}]}
