"""Runtime function execution endpoints."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any, Optional

from app.core.auth import (
    get_current_user_with_permissions,
    get_execution_depth_from_request,
    set_permission_used,
)
from app.core.callback import CallbackURLError, validate_callback_url
from app.core.config import settings
from app.core.database import get_db
from app.core.permissions import check_permission


def _resolve_child_depth(request: Request) -> int:
    """Depth of the execution this request is about to create.

    Top-level (real user/app token) → 0; a call made from inside a running
    execution → caller depth + 1. Rejects past settings.max_execution_depth so
    runaway / over-nested chains fail fast instead of exhausting the worker pool.
    """
    caller_depth = get_execution_depth_from_request(request)
    depth = 0 if caller_depth is None else caller_depth + 1
    if settings.max_execution_depth and depth > settings.max_execution_depth:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Maximum execution nesting depth ({settings.max_execution_depth}) exceeded. "
                "A function or agent invoked another execution too deeply — usually unintended "
                "recursion or over-nested shared_pool calls. Flatten the chain, or raise "
                "MAX_EXECUTION_DEPTH (and the shared worker pool) if this nesting is intended."
            ),
        )
    return depth
from app.models.execution import TriggerType
from app.models.function import Function
from app.schemas.batch import FunctionBatchRequest, FunctionBatchResponse
from app.schemas.function import (
    FunctionExecuteAsyncResponse,
    FunctionExecuteRequest,
    FunctionExecuteResponse,
)
from app.services import batch_service
from app.services.queue_service import queue_service

router = APIRouter()


@router.post(
    "/functions/{namespace}/{name}/execute",
    response_model=FunctionExecuteResponse,
)
async def execute_function(
    request: Request,
    namespace: str,
    name: str,
    body: FunctionExecuteRequest,
    current_user_data: tuple = Depends(get_current_user_with_permissions),
    db: AsyncSession = Depends(get_db),
):
    """
    Execute a function and wait for the result.

    Enqueues the function on the worker queue and blocks until the result
    is available or the timeout is reached.
    """
    user_id, permissions = current_user_data

    function = await Function.get_by_name(db, namespace, name)
    if not function:
        raise HTTPException(status_code=404, detail="Function not found")

    permission = f"sinas.functions/{namespace}/{name}.execute:own"
    if not check_permission(permissions, permission):
        set_permission_used(request, permission, has_perm=False)
        raise HTTPException(status_code=403, detail="Not authorized to execute this function")

    set_permission_used(request, permission)

    depth = _resolve_child_depth(request)
    execution_id = str(uuid.uuid4())

    try:
        result = await queue_service.enqueue_and_wait(
            function_namespace=namespace,
            function_name=name,
            input_data=body.input,
            execution_id=execution_id,
            trigger_type=TriggerType.API.value,
            trigger_id="runtime-api",
            user_id=user_id,
            timeout=body.timeout,
            depth=depth,
        )

        return FunctionExecuteResponse(
            status="success",
            execution_id=execution_id,
            result=result,
        )
    except TimeoutError:
        return FunctionExecuteResponse(
            status="timeout",
            execution_id=execution_id,
            error="Function execution timed out. Poll GET /executions/{execution_id} for status.",
        )
    except Exception as e:
        return FunctionExecuteResponse(
            status="error",
            execution_id=execution_id,
            error=str(e),
        )


@router.post(
    "/functions/{namespace}/{name}/execute/async",
    response_model=FunctionExecuteAsyncResponse,
    status_code=202,
)
async def execute_function_async(
    request: Request,
    namespace: str,
    name: str,
    body: FunctionExecuteRequest,
    current_user_data: tuple = Depends(get_current_user_with_permissions),
    db: AsyncSession = Depends(get_db),
):
    """
    Execute a function asynchronously (fire-and-forget).

    Enqueues the function and returns immediately with an execution_id.
    Poll GET /executions/{execution_id} for status and result.
    """
    user_id, permissions = current_user_data

    function = await Function.get_by_name(db, namespace, name)
    if not function:
        raise HTTPException(status_code=404, detail="Function not found")

    permission = f"sinas.functions/{namespace}/{name}.execute:own"
    if not check_permission(permissions, permission):
        set_permission_used(request, permission, has_perm=False)
        raise HTTPException(status_code=403, detail="Not authorized to execute this function")

    set_permission_used(request, permission)

    try:
        callback_url = validate_callback_url(body.callback_url)
    except CallbackURLError as e:
        raise HTTPException(status_code=400, detail=str(e))

    depth = _resolve_child_depth(request)
    execution_id = str(uuid.uuid4())

    await queue_service.enqueue_function(
        function_namespace=namespace,
        function_name=name,
        input_data=body.input,
        execution_id=execution_id,
        trigger_type=TriggerType.API.value,
        trigger_id=body.trigger_id or "runtime-api",
        user_id=user_id,
        delay=body.delay_seconds,
        callback_url=callback_url,
        depth=depth,
    )

    return FunctionExecuteAsyncResponse(
        execution_id=execution_id,
    )


@router.post(
    "/functions/{namespace}/{name}/execute/batch",
    response_model=FunctionBatchResponse,
    status_code=202,
)
async def execute_function_batch(
    request: Request,
    namespace: str,
    name: str,
    body: FunctionBatchRequest,
    current_user_data: tuple = Depends(get_current_user_with_permissions),
    db: AsyncSession = Depends(get_db),
):
    """Submit N function executions as a single batch.

    Each `inputs[i]` becomes one execution. Track aggregate progress via
    `GET /batches/{batch_id}`. See ADR 2026-05-14-external-app-bulk-enqueue.md
    §2.4.
    """
    user_id, permissions = current_user_data

    function = await Function.get_by_name(db, namespace, name)
    if not function:
        raise HTTPException(status_code=404, detail="Function not found")

    permission = f"sinas.functions/{namespace}/{name}.execute:own"
    if not check_permission(permissions, permission):
        set_permission_used(request, permission, has_perm=False)
        raise HTTPException(status_code=403, detail="Not authorized to execute this function")
    set_permission_used(request, permission)

    try:
        callback_url = validate_callback_url(body.callback_url)
        batch_callback_url = validate_callback_url(body.batch_callback_url)
    except CallbackURLError as e:
        raise HTTPException(status_code=400, detail=str(e))

    depth = _resolve_child_depth(request)

    try:
        result = await batch_service.submit_function_batch(
            db=db,
            user_id=user_id,
            namespace=namespace,
            name=name,
            inputs=body.inputs,
            trigger_id_prefix=body.trigger_id_prefix,
            delay_seconds=body.delay_seconds,
            callback_url=callback_url,
            batch_callback_url=batch_callback_url,
            depth=depth,
        )
    except batch_service.BatchError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return FunctionBatchResponse(**result)
