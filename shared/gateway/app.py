"""FastAPI process: the one thing that runs in the container next to gowa
(Spec: "operators... one Docker container"). Phase 0: webhook intake only."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request, Response

from shared.db import init_db
from shared.gateway.interface import receive_webhook


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="watcher-gateway", lifespan=lifespan)


@app.post("/webhook/gowa")
async def gowa_webhook(request: Request, x_hub_signature_256: str | None = Header(default=None)) -> Response:
    raw_body = await request.body()
    accepted = await receive_webhook(raw_body, x_hub_signature_256)
    if not accepted:
        return Response(status_code=400)
    return Response(status_code=200)


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}
