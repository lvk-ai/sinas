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
from app.services.chat_wakeups import handle_watch_delivery, watch_status

router = APIRouter(tags=["runtime-watch"])


@router.post("/watch/{token}")
async def deliver_watch(token: str, request: Request):
    # Cheap checks before any body is read: an unknown or dead token costs
    # one indexed lookup, and the body is read in chunks and abandoned the
    # moment it passes the cap, so a stranger cannot make the backend
    # buffer a large request.
    problem = await watch_status(token)
    if problem:
        status, out = problem
        return JSONResponse(status_code=status, content=out)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.watch_max_payload_bytes:
        raise HTTPException(status_code=413, detail="Payload too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > settings.watch_max_payload_bytes:
            raise HTTPException(status_code=413, detail="Payload too large")
        chunks.append(chunk)
    text = b"".join(chunks).decode("utf-8", errors="replace")
    try:
        payload = json.loads(text) if text.strip() else {}
    except ValueError:
        payload = text
    status, out = await handle_watch_delivery(token, payload)
    return JSONResponse(status_code=status, content=out)
