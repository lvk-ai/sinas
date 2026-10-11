"""Public callback URLs for chat watches (create_watch, ADR
2026-10-11-background-jobs-and-wakeups).

No authentication: the token in the path is the secret (32 random bytes,
per watch, expiring). The body is delivered into the chat verbatim (JSON
when it parses, text otherwise), capped at watch_max_payload_bytes.
"""
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.services.chat_wakeups import handle_watch_delivery

router = APIRouter(tags=["runtime-watch"])


@router.post("/watch/{token}")
async def deliver_watch(token: str, request: Request):
    body = await request.body()
    if len(body) > settings.watch_max_payload_bytes:
        raise HTTPException(status_code=413, detail="Payload too large")
    text = body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text) if text.strip() else {}
    except ValueError:
        payload = text
    status, out = await handle_watch_delivery(token, payload)
    return JSONResponse(status_code=status, content=out)
