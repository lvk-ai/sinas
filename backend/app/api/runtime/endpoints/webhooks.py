"""Runtime webhook endpoints - execute functions or agents via HTTP."""
import asyncio
import json
import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import set_permission_used, verify_jwt_or_api_key
from app.core.database import get_db
from app.core.permissions import check_permission
from app.models.execution import TriggerType
from app.models.webhook import Webhook
from app.services.dedup_service import check_and_mark, store_result
from app.services.queue_service import queue_service

logger = logging.getLogger(__name__)

router = APIRouter()

# Reserved keys for the raw response-control convention:
# a function may return {"_status": 200, "_headers": {...}, "_body": ...}
RAW_CONTROL_KEYS = ("_status", "_headers", "_body")


def _build_raw_response(result: Any) -> tuple[Response, dict[str, Any]]:
    """Build the HTTP response for a raw-mode webhook from a function result.

    Returns (response, cache_entry) where cache_entry is the JSON-serializable
    form used for dedup replay.
    """
    status = 200
    headers: dict[str, str] = {}
    body = result

    if isinstance(result, dict) and any(k in result for k in RAW_CONTROL_KEYS):
        raw_status = result.get("_status", 200)
        status = raw_status if isinstance(raw_status, int) else 200
        raw_headers = result.get("_headers")
        if isinstance(raw_headers, dict):
            headers = {str(k): str(v) for k, v in raw_headers.items()}
        body = result.get("_body")

    cache_entry = {"__raw__": {"status": status, "headers": headers, "body": body}}

    if isinstance(body, str):
        return PlainTextResponse(body, status_code=status, headers=headers), cache_entry
    return JSONResponse(body, status_code=status, headers=headers), cache_entry


def _replay_cached(cached: str) -> Response:
    """Rebuild the HTTP response for a deduplicated request from the cache."""
    parsed = json.loads(cached)
    if isinstance(parsed, dict) and "__raw__" in parsed:
        raw = parsed["__raw__"]
        body = raw.get("body")
        status = raw.get("status", 200)
        headers = raw.get("headers") or {}
        if isinstance(body, str):
            return PlainTextResponse(body, status_code=status, headers=headers)
        return JSONResponse(body, status_code=status, headers=headers)
    return JSONResponse(parsed, status_code=200)


async def _execute_agent_webhook(
    webhook: Webhook,
    db: AsyncSession,
    user_id: str,
    final_input: Any,
    req_headers: dict[str, str],
):
    """Execute an agent-target webhook: render templates, resolve the chat by
    session key, and either enqueue (async) or wait for the reply (sync)."""
    from app.core.auth import create_access_token
    from app.core.config import settings
    from app.models.agent import Agent
    from app.models.chat import Chat
    from app.models.user import User
    from app.services.message_service import MessageService
    from app.services.template_renderer import render_webhook_template

    agent_namespace = webhook.agent_namespace or "default"
    agent = await Agent.get_by_name(db, agent_namespace, webhook.agent_name)
    if not agent:
        raise HTTPException(
            status_code=500,
            detail=f"Webhook target agent '{agent_namespace}/{webhook.agent_name}' not found",
        )

    # Render templates against the request payload (defaults merged in)
    context = final_input if isinstance(final_input, dict) else {"input": final_input}
    message = render_webhook_template(webhook.message_template or "", context).strip()
    if not message:
        logger.warning(
            "Webhook %s: message template rendered empty, falling back to raw payload",
            webhook.path,
        )
        message = json.dumps(context)

    session_key: Optional[str] = None
    if webhook.session_key_template:
        session_key = render_webhook_template(webhook.session_key_template, context).strip() or None

    # Look up user (needed for the JWT the agent runs with)
    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=500, detail="Webhook user not found")
    token = create_access_token(user_id=user_id, email=user.email)

    # Resolve or create the chat (session-key continuity, like agent invoke)
    chat = None
    if session_key:
        result = await db.execute(
            select(Chat).where(
                Chat.agent_id == agent.id,
                Chat.session_key == session_key,
                Chat.archived == False,
            )
        )
        chat = result.scalar_one_or_none()
        if chat and str(chat.user_id) != user_id:
            raise HTTPException(status_code=403, detail="Not authorized to use this session")

    if not chat:
        chat = Chat(
            user_id=user_id,
            agent_id=agent.id,
            agent_namespace=agent.namespace,
            agent_name=agent.name,
            title=f"webhook:{webhook.path}",
            session_key=session_key,
            job_timeout=agent.default_job_timeout,
        )
        db.add(chat)
        await db.flush()
        await db.refresh(chat)

    chat_id = str(chat.id)

    # Async mode: commit the chat so the queue worker can see it, then enqueue
    if webhook.response_mode == "async":
        await db.commit()
        job_id = await queue_service.enqueue_agent_message(
            chat_id=chat_id,
            user_id=user_id,
            user_token=token,
            content=message,
            channel_id=str(uuid.uuid4()),
            agent=f"{agent.namespace}/{agent.name}",
            trigger_type=TriggerType.WEBHOOK.value,
            job_timeout=agent.default_job_timeout,
        )
        return JSONResponse({"chat_id": chat_id, "job_id": job_id}, status_code=202)

    # Sync mode: run inline and wait for the reply (same as agent invoke)
    message_service = MessageService(db)
    timeout = agent.default_job_timeout or settings.function_timeout or 300
    try:
        response_message = await asyncio.wait_for(
            message_service.send_message(
                chat_id=chat_id,
                user_id=user_id,
                user_token=token,
                content=message,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Agent execution timed out")

    response = {
        "success": True,
        "chat_id": chat_id,
        "reply": response_message.content or "",
    }

    # Cache result for dedup
    if webhook.dedup:
        try:
            await store_result(
                webhook_id=str(webhook.id),
                body=final_input if isinstance(final_input, dict) else {},
                headers=req_headers,
                dedup_config=webhook.dedup,
                result=json.dumps(response),
            )
        except Exception:
            pass  # Non-critical

    return response


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def execute_webhook(
    path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Execute webhook by triggering the associated function or agent."""
    # Look up webhook configuration
    result = await db.execute(
        select(Webhook).where(
            and_(
                Webhook.path == path,
                Webhook.http_method == request.method,
                Webhook.is_active == True,
            )
        )
    )
    webhook = result.scalar_one_or_none()

    if not webhook:
        raise HTTPException(
            status_code=404,
            detail=f"No active webhook found for path '{path}' and method '{request.method}'",
        )

    is_agent_target = webhook.target_type == "agent"

    # Authenticate if required
    user_id: Optional[str] = None
    if webhook.requires_auth:
        auth_header = request.headers.get("authorization")
        api_key_header = request.headers.get("x-api-key")

        if not auth_header and not api_key_header:
            raise HTTPException(status_code=401, detail="Authorization required")

        try:
            # Build credentials in the format verify_jwt_or_api_key expects
            from fastapi.security import HTTPAuthorizationCredentials

            credentials = None
            if auth_header and auth_header.lower().startswith("bearer "):
                credentials = HTTPAuthorizationCredentials(
                    scheme="Bearer", credentials=auth_header[7:]
                )

            user_id, email, permissions = await verify_jwt_or_api_key(
                credentials=credentials,
                x_api_key=api_key_header,
                db=db,
            )

            # Check permission on the target resource
            if is_agent_target:
                target_perm = (
                    f"sinas.agents/{webhook.agent_namespace}/{webhook.agent_name}.chat:own"
                )
                target_perm_all = (
                    f"sinas.agents/{webhook.agent_namespace}/{webhook.agent_name}.chat:all"
                )
            else:
                target_perm = f"sinas.functions/{webhook.function_namespace}/{webhook.function_name}.execute:own"
                target_perm_all = f"sinas.functions/{webhook.function_namespace}/{webhook.function_name}.execute:all"

            has_permission = check_permission(permissions, target_perm_all) or (
                check_permission(permissions, target_perm) and str(webhook.user_id) == user_id
            )

            if not has_permission:
                set_permission_used(request, target_perm, has_perm=False)
                raise HTTPException(status_code=403, detail=f"Not authorized to execute webhook '{path}'")

            set_permission_used(
                request,
                target_perm_all if check_permission(permissions, target_perm_all) else target_perm,
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"Authentication failed: {str(e)}")
    else:
        # Use webhook owner's user_id for unauthenticated webhooks
        user_id = str(webhook.user_id)
        set_permission_used(request, f"webhook.public:{webhook.path}")

    try:
        # Extract the request body as input
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                input_data = await request.json()
            except Exception:
                input_data = {}
        elif request.method == "GET":
            input_data = dict(request.query_params)
        else:
            input_data = {}

        # Merge default values (body overrides defaults)
        if webhook.default_values:
            final_input = {**webhook.default_values, **(input_data if isinstance(input_data, dict) else {"input": input_data})}
        else:
            final_input = input_data

        # Deduplication check (identical for function and agent targets)
        req_headers = dict(request.headers)
        if webhook.dedup:
            is_dup, cached = await check_and_mark(
                webhook_id=str(webhook.id),
                body=final_input if isinstance(final_input, dict) else {},
                headers=req_headers,
                dedup_config=webhook.dedup,
            )
            if is_dup:
                if cached:
                    return _replay_cached(cached)
                return JSONResponse({"deduplicated": True}, status_code=200)

        # Agent target
        if is_agent_target:
            return await _execute_agent_webhook(
                webhook=webhook,
                db=db,
                user_id=user_id,
                final_input=final_input,
                req_headers=req_headers,
            )

        # Function target
        execution_id = str(uuid.uuid4())
        chat_id = request.headers.get("x-chat-id")

        # Async mode: return immediately
        if webhook.response_mode == "async":
            await queue_service.enqueue_function(
                function_namespace=webhook.function_namespace,
                function_name=webhook.function_name,
                input_data=final_input,
                execution_id=execution_id,
                trigger_type=TriggerType.WEBHOOK.value,
                trigger_id=str(webhook.id),
                user_id=user_id,
                chat_id=chat_id,
            )
            return JSONResponse({"execution_id": execution_id}, status_code=202)

        # Sync and raw modes: wait for result
        result = await queue_service.enqueue_and_wait(
            function_namespace=webhook.function_namespace,
            function_name=webhook.function_name,
            input_data=final_input,
            execution_id=execution_id,
            trigger_type=TriggerType.WEBHOOK.value,
            trigger_id=str(webhook.id),
            user_id=user_id,
            chat_id=chat_id,
        )

        # Raw mode: the function's return value IS the response body
        if webhook.response_mode == "raw":
            raw_response, cache_entry = _build_raw_response(result)
            if webhook.dedup:
                try:
                    await store_result(
                        webhook_id=str(webhook.id),
                        body=final_input if isinstance(final_input, dict) else {},
                        headers=req_headers,
                        dedup_config=webhook.dedup,
                        result=json.dumps(cache_entry),
                    )
                except Exception:
                    pass  # Non-critical
            return raw_response

        # Sync mode (default): wrap in the standard envelope
        response = {"success": True, "execution_id": execution_id, "result": result}

        # Cache result for dedup
        if webhook.dedup:
            try:
                await store_result(
                    webhook_id=str(webhook.id),
                    body=final_input if isinstance(final_input, dict) else {},
                    headers=req_headers,
                    dedup_config=webhook.dedup,
                    result=json.dumps(response),
                )
            except Exception:
                pass  # Non-critical

        return response

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Webhook execution failed: {str(e)}")
