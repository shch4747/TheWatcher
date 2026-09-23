"""Thin HTTP adapter (Spec, Shape: "exposes the same interfaces as JSON
endpoints (and later MCP tools) for out-of-process callers. v1 has no
out-of-process callers; the adapter exists so the contract is
exercised."). Every route is a direct pass-through to an existing
package interface function - no new logic lives here.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from shared.gateway.interface import get_channel, get_message, get_messages, list_channels
from shared.gateway.interface import send as gateway_send
from shared.scheduler.interface import due_jobs
from shared.wiki.interface import LintIssue, lint_text

router = APIRouter(prefix="/api")


class SendRequest(BaseModel):
    channel: str
    text: str
    reply_to: str | None = None


class SendResponse(BaseModel):
    message_id: str | None


@router.post("/gateway/send", response_model=SendResponse)
async def http_send(body: SendRequest) -> SendResponse:
    result = await gateway_send(body.channel, body.text, reply_to=body.reply_to)
    return SendResponse(message_id=result.message_id)


@router.get("/gateway/messages/{message_id}")
async def http_get_message(message_id: str) -> dict:
    message = await get_message(message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="message not found")
    return message.model_dump(mode="json")


@router.get("/gateway/channels/{channel_jid}/messages")
async def http_get_messages(channel_jid: str, since_id: str | None = None, limit: int = 100) -> list[dict]:
    messages = await get_messages(channel_jid, since_id=since_id, limit=limit)
    return [m.model_dump(mode="json") for m in messages]


class ChannelResponse(BaseModel):
    jid: str
    kind: str
    title: str | None
    initiative: str | None


@router.get("/gateway/channels", response_model=list[ChannelResponse])
async def http_list_channels(kind: str | None = None) -> list[ChannelResponse]:
    channels = await list_channels(kind=kind)
    return [
        ChannelResponse(jid=c.jid, kind=c.kind, title=c.title, initiative=c.initiative) for c in channels
    ]


@router.get("/gateway/channels/{channel_jid}", response_model=ChannelResponse)
async def http_get_channel(channel_jid: str) -> ChannelResponse:
    channel = await get_channel(channel_jid)
    if channel is None:
        raise HTTPException(status_code=404, detail="channel not found")
    return ChannelResponse(
        jid=channel.jid, kind=channel.kind, title=channel.title, initiative=channel.initiative
    )


class LintRequest(BaseModel):
    path: str
    text: str


class LintIssueResponse(BaseModel):
    path: str
    severity: str
    message: str


@router.post("/wiki/lint", response_model=list[LintIssueResponse])
async def http_lint(body: LintRequest) -> list[LintIssueResponse]:
    issues: list[LintIssue] = lint_text(body.path, body.text)
    return [LintIssueResponse(path=i.path, severity=i.severity, message=i.message) for i in issues]


@router.get("/scheduler/due-jobs")
async def http_due_jobs() -> list[str]:
    return await due_jobs()
