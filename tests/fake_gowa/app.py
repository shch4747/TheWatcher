"""Minimal fake gowa: serves the /send/message shape Phase 0 needs.
Grows to cover /chat/{jid}/messages, /message/{id}/reaction etc. as later
phases need them (Spec, Testing Decisions, Seam 1)."""
from __future__ import annotations

import uuid

from fastapi import FastAPI, Request

app = FastAPI(title="fake-gowa")
app.state.sent_messages: list[dict] = []


@app.post("/send/message")
async def send_message(request: Request) -> dict:
    body = await request.json()
    message_id = f"fake-{uuid.uuid4().hex[:12]}"
    app.state.sent_messages.append({**body, "message_id": message_id})
    return {"results": {"message_id": message_id}}


@app.get("/app/devices")
async def devices() -> dict:
    return {"results": [{"device": "fake", "status": "connected"}]}
