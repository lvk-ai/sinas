"""Function execution engine with tracking and validation."""
import asyncio
import json
import logging
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx
import jsonschema
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.execution import Execution, ExecutionStatus
from app.models.function import Function
from app.services.clickhouse_logger import clickhouse_logger


logger = logging.getLogger(__name__)


class FunctionExecutionError(Exception):
    pass


class SchemaValidationError(Exception):
    pass


async def _fire_callback(
    *,
    db: AsyncSession,
    execution: "Execution",
    callback_url: str,
    user_id: str,
    user_email: str,
    status: str,  # "success" | "failure"
    result: Any,
    error: Optional[str],
    trigger_id: str,
    function_namespace: str,
    function_name: str,
) -> None:
    """Fire-and-forget POST to the caller-supplied callback URL.

    Authenticated with a freshly-minted user access token so the receiving
    app can validate via the normal Sinas auth path. No retries — apps
    fall back to polling `/executions/{id}/status` if they need to
    reconcile.

    Records `callback_status` and `callback_response_code` on the
    Execution row for operator visibility.
    """
    from app.core.auth import create_access_token

    # Short callback-only token (just long enough to be received and validated).
    cb_token = create_access_token(user_id, user_email, expires_delta=timedelta(minutes=5))

    payload = {
        "execution_id": execution.execution_id,
        "trigger_id": trigger_id,
        "function": {"namespace": function_namespace, "name": function_name},
        "status": status,
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "finished_at": execution.completed_at.isoformat() if execution.completed_at else None,
        "duration_ms": execution.duration_ms,
        "result": result,
        "error": error,
    }

    status_label = "failed"
    response_code: Optional[int] = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                callback_url,
                headers={
                    "Authorization": f"Bearer {cb_token}",
                    "Content-Type": "application/json",
                },
                content=json.dumps(payload, default=str),
            )
        response_code = resp.status_code
        status_label = "sent" if 200 <= resp.status_code < 300 else "failed"
    except httpx.HTTPError as e:
        logger.warning(
            "Callback POST failed for execution %s → %s: %s",
            execution.execution_id, callback_url, e,
        )
    except Exception as e:
        logger.warning(
            "Unexpected callback error for execution %s → %s: %s",
            execution.execution_id, callback_url, e,
        )

    try:
        execution.callback_status = status_label
        execution.callback_response_code = response_code
        await db.commit()
    except Exception:
        # Don't let callback bookkeeping mask the underlying execution result.
        logger.exception("Failed to persist callback status for %s", execution.execution_id)


class FunctionExecutor:
    def __init__(self):
        self.functions_cache: dict[str, Function] = {}
        self._container_pool = None

    @property
    def container_pool(self):
        """Lazy load container pool (replaces per-user containers)."""
        if self._container_pool is None:
            from app.services.container_pool import container_pool

            self._container_pool = container_pool
        return self._container_pool

    @property
    def worker_manager(self):
        """Lazy load shared worker manager."""
        if not hasattr(self, "_worker_manager") or self._worker_manager is None:
            from app.services.shared_worker_manager import shared_worker_manager

            self._worker_manager = shared_worker_manager
        return self._worker_manager

    async def validate_schema(self, data: Any, schema: dict[str, Any]) -> Any:
        """
        Validate data against JSON schema with type coercion.

        Returns:
            Coerced data
        """
        from app.utils.schema import validate_with_coercion

        try:
            return validate_with_coercion(data, schema)
        except jsonschema.ValidationError as e:
            raise SchemaValidationError(f"Schema validation failed: {e.message}")

    async def _execute_in_shared_pool(
        self,
        function: Function,
        input_data: dict[str, Any],
        execution_id: str,
        user_id: str,
        user_email: str,
        access_token: str,
        trigger_type: str,
        chat_id: Optional[str],
        db: AsyncSession,
        timeout: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Execute function in shared worker pool (separate worker containers).

        SECURITY: Only use for trusted, admin-created functions.
        Workers are separate containers from backend but shared across users.
        No per-user isolation - functions must be trusted.
        """
        exec_result = await self.worker_manager.execute_function(
            user_id=user_id,
            user_email=user_email,
            access_token=access_token,
            function_namespace=function.namespace,
            function_name=function.name,
            input_data=input_data,
            execution_id=execution_id,
            trigger_type=trigger_type,
            chat_id=chat_id,
            db=db,
            timeout=timeout,
        )

        return exec_result

    async def load_function(
        self, db: AsyncSession, function_namespace: str, function_name: str, user_id: str
    ) -> Function:
        """Load function from database with caching.

        Cache is invalidated when the function's updated_at changes,
        so code/config edits are picked up without process restart.

        Note: No permission checks here - permissions should be validated at entry points
        (agent tool execution, webhook validation, schedule authorization).
        """
        cache_key = f"{function_namespace}:{function_name}"

        result = await db.execute(
            select(Function).where(
                Function.namespace == function_namespace,
                Function.name == function_name,
                Function.is_active == True,
            )
        )
        function = result.scalar_one_or_none()

        if not function:
            raise FunctionExecutionError(
                f"Function '{function_namespace}/{function_name}' not found or inactive"
            )

        # Invalidate cache if function was updated since last load
        cached = self.functions_cache.get(cache_key)
        if cached and cached.updated_at != function.updated_at:
            logger.info(f"Function cache invalidated: {cache_key} (updated)")

        self.functions_cache[cache_key] = function
        return function

    async def execute_function(
        self,
        function_namespace: str,
        function_name: str,
        input_data: dict[str, Any],
        execution_id: str,
        trigger_type: str,
        trigger_id: str,
        user_id: str,
        chat_id: Optional[str] = None,
        callback_url: Optional[str] = None,
        depth: int = 0,
    ) -> dict[str, Any]:
        """Execute a function with input validation and tracking.

        `depth` is this execution's nesting depth (0 = top-level). It is
        embedded in the per-execution access token so a nested call can
        compute its own depth and be bounded by settings.max_execution_depth.
        """
        async with AsyncSessionLocal() as db:
            # Get user info for context
            from app.core.auth import create_access_token
            from app.models.user import User

            user_result = await db.execute(select(User).where(User.id == user_id))
            user = user_result.scalar_one_or_none()
            user_email = user.email if user else "unknown@unknown.com"

            # Load function early so we can size the access-token TTL to its
            # configured timeout. (load_function is cached, so this is cheap.)
            function = await self.load_function(db, function_namespace, function_name, user_id)
            function_timeout = function.timeout or settings.function_timeout

            # Per-execution access token. TTL = function timeout + buffer, so
            # the token outlives the run (incl. downstream calls into apps that
            # forward it back to Sinas) but not by much. Capped at the global
            # ceiling. See ADR 2026-05-14-external-app-bulk-enqueue.md §2.2.
            token_ttl = timedelta(
                seconds=min(
                    function_timeout + settings.execution_token_buffer_seconds,
                    settings.max_execution_token_seconds,
                )
            )
            access_token = create_access_token(
                user_id, user_email, expires_delta=token_ttl, execution_depth=depth
            )

            # Check if execution already exists (e.g. created by enqueue_function)
            result = await db.execute(
                select(Execution).where(Execution.execution_id == execution_id)
            )
            execution = result.scalar_one_or_none()

            if not execution:
                # Create new execution record
                execution = Execution(
                    user_id=user_id,
                    execution_id=execution_id,
                    function_name=function_name,
                    trigger_type=trigger_type,
                    trigger_id=trigger_id,
                    chat_id=chat_id,
                    status=ExecutionStatus.RUNNING,
                    input_data=input_data,
                    started_at=datetime.utcnow(),
                )
                db.add(execution)
                await db.commit()

                # Log execution start to Redis
                await clickhouse_logger.log_execution_start(execution_id, function_name, input_data)

            try:
                # `function` and `function_timeout` were loaded above (they
                # parameterized the access-token TTL).

                # Validate input
                if function.input_schema:
                    # Validate and coerce types (handles string -> number, etc.)
                    input_data = await self.validate_schema(input_data, function.input_schema)

                start_time = time.time()

                # Route execution based on shared_pool setting
                if function.shared_pool:
                    # Execute in shared worker container pool
                    print(
                        f"⏱️  [TIMING] Executing {function_namespace}/{function_name} in shared pool"
                    )
                    exec_result = await self._execute_in_shared_pool(
                        function=function,
                        input_data=input_data,
                        execution_id=execution_id,
                        user_id=user_id,
                        user_email=user_email,
                        access_token=access_token,
                        trigger_type=trigger_type,
                        chat_id=chat_id,
                        db=db,
                        timeout=function_timeout,
                    )
                    elapsed = time.time() - start_time
                    print(f"⏱️  [TIMING] Shared pool execution completed in {elapsed:.3f}s")
                else:
                    # Execute in pooled Docker container (untrusted code)
                    print(
                        f"⏱️  [TIMING] Executing {function_namespace}/{function_name} in sandbox container"
                    )
                    container_start = time.time()
                    exec_result = await self.container_pool.execute_function(
                        user_id=user_id,
                        user_email=user_email,
                        access_token=access_token,
                        function_namespace=function_namespace,
                        function_name=function_name,
                        input_data=input_data,
                        execution_id=execution_id,
                        trigger_type=trigger_type,
                        chat_id=chat_id,
                        db=db,
                        timeout=function_timeout,
                    )
                    container_elapsed = time.time() - container_start
                    print(f"⏱️  [TIMING] Sandbox container execution completed in {container_elapsed:.3f}s")

                # Handle awaiting_input from shared containers
                if exec_result.get("status") == "awaiting_input":
                    execution.status = ExecutionStatus.AWAITING_INPUT
                    execution.input_prompt = exec_result.get("prompt")
                    execution.container_id = exec_result.get("container_name")
                    await db.commit()
                    return {
                        "status": "awaiting_input",
                        "execution_id": execution_id,
                        "prompt": execution.input_prompt,
                    }

                if exec_result.get("status") == "failed":
                    err = FunctionExecutionError(exec_result.get("error", "Unknown error"))
                    # The container captured the function's real internal traceback.
                    # Attach it so the except handler persists it instead of the
                    # backend's own (useless) stack pointing at this raise.
                    err.container_traceback = exec_result.get("traceback")
                    raise err

                result = exec_result.get("result")
                duration_ms = exec_result.get("duration_ms", 0)

                # Validate output
                if function.output_schema:
                    await self.validate_schema(result, function.output_schema)

                # Update execution record
                execution.status = ExecutionStatus.COMPLETED
                execution.output_data = result
                execution.completed_at = datetime.utcnow()
                execution.duration_ms = duration_ms

                await db.commit()

                # Log execution completion to Redis
                await clickhouse_logger.log_execution_end(
                    execution_id, "completed", result, None, duration_ms
                )

                if callback_url:
                    await _fire_callback(
                        db=db,
                        execution=execution,
                        callback_url=callback_url,
                        user_id=user_id,
                        user_email=user_email,
                        status="success",
                        result=result,
                        error=None,
                        trigger_id=trigger_id,
                        function_namespace=function_namespace,
                        function_name=function_name,
                    )

                if execution.batch_id is not None:
                    from app.services import batch_service
                    await batch_service.on_execution_terminated(
                        db=db, batch_id=execution.batch_id,
                    )

                return result

            except Exception as e:
                # Update execution record with error
                execution.status = ExecutionStatus.FAILED
                execution.error = str(e)
                # Prefer the function's real traceback from the container; fall
                # back to the backend stack for errors raised outside the function
                # (schema validation, db, container plumbing, etc.).
                execution.traceback = getattr(e, "container_traceback", None) or traceback.format_exc()
                execution.completed_at = datetime.utcnow()

                await db.commit()

                # Log execution failure to Redis
                await clickhouse_logger.log_execution_end(
                    execution_id, "failed", None, str(e), None
                )

                if callback_url:
                    await _fire_callback(
                        db=db,
                        execution=execution,
                        callback_url=callback_url,
                        user_id=user_id,
                        user_email=user_email,
                        status="failure",
                        result=None,
                        error=str(e),
                        trigger_id=trigger_id,
                        function_namespace=function_namespace,
                        function_name=function_name,
                    )

                if execution.batch_id is not None:
                    from app.services import batch_service
                    await batch_service.on_execution_terminated(
                        db=db, batch_id=execution.batch_id,
                    )

                raise FunctionExecutionError(f"Function execution failed: {e}")

    async def resume_execution(
        self,
        execution_id: str,
        resume_value: Any,
    ) -> dict[str, Any]:
        """
        Resume a paused execution by writing the resume value directly
        to the container where the function is waiting on input().

        Bypasses the queue — the function thread is already running.
        """
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Execution).where(Execution.execution_id == execution_id)
            )
            execution = result.scalar_one_or_none()

            if not execution:
                raise FunctionExecutionError(f"Execution {execution_id} not found")

            if execution.status != ExecutionStatus.AWAITING_INPUT:
                raise FunctionExecutionError(
                    f"Execution {execution_id} is not awaiting input (status={execution.status})"
                )

            container_id = execution.container_id
            if not container_id:
                raise FunctionExecutionError(
                    f"Execution {execution_id} has no container_id — cannot resume"
                )

            # Write resume file into the container
            import docker

            client = docker.from_env()
            try:
                container = client.containers.get(container_id)
            except docker.errors.NotFound:
                # Container gone — mark execution as failed
                execution.status = ExecutionStatus.FAILED
                execution.error = "Container no longer available (restarted?)"
                execution.completed_at = datetime.utcnow()
                await db.commit()
                raise FunctionExecutionError(
                    f"Container {container_id} not found — execution cannot be resumed"
                )

            # Write resume data into the container via stdin pipe
            resume_payload = json.dumps({"value": resume_value}).encode("utf-8")
            resume_file = f"/tmp/exec_resume_{execution_id}.json"

            api = container.client.api
            exec_id = api.exec_create(
                container.id,
                [
                    "python3", "-c",
                    f'import sys; open("{resume_file}","wb").write(sys.stdin.buffer.read())',
                ],
                stdin=True,
                stdout=True,
                stderr=True,
            )["Id"]
            sock = api.exec_start(exec_id, socket=True)
            sock._sock.sendall(resume_payload)
            import socket as _sock_mod
            sock._sock.shutdown(_sock_mod.SHUT_WR)
            sock.read()
            sock.close()

            execution.status = ExecutionStatus.RUNNING
            await db.commit()

            # Poll for result from the container
            result_file = f"/tmp/exec_result_{execution_id}.json"
            function_timeout = settings.function_timeout

            exec_result = await asyncio.to_thread(
                container.exec_run,
                cmd=[
                    "python3",
                    "-c",
                    f"""
import sys, json, time, os
max_wait = {function_timeout}
start = time.time()
while time.time() - start < max_wait:
    try:
        with open("{result_file}", "r") as f:
            result = json.load(f)
            print(json.dumps(result))
            sys.exit(0)
    except FileNotFoundError:
        time.sleep(0.1)
        continue
print(json.dumps({{"error": "Resume timeout after {function_timeout}s"}}))
sys.exit(1)
""",
                ],
                demux=True,
            )

            stdout, stderr = exec_result.output
            stdout_str = stdout.decode() if stdout else ""

            if exec_result.exit_code != 0:
                stderr_str = stderr.decode() if stderr else ""
                error_msg = stderr_str or stdout_str or f"Exit code {exec_result.exit_code}"
                execution.status = ExecutionStatus.FAILED
                execution.error = error_msg
                execution.completed_at = datetime.utcnow()
                await db.commit()
                raise FunctionExecutionError(f"Resume failed: {error_msg}")

            result_data = json.loads(stdout_str)

            # Handle another awaiting_input (multiple input() calls)
            if result_data.get("status") == "awaiting_input":
                execution.status = ExecutionStatus.AWAITING_INPUT
                execution.input_prompt = result_data.get("prompt")
                await db.commit()
                return {
                    "status": "awaiting_input",
                    "execution_id": execution_id,
                    "prompt": execution.input_prompt,
                }

            if result_data.get("status") == "failed":
                execution.status = ExecutionStatus.FAILED
                execution.error = result_data.get("error", "Unknown error")
                execution.traceback = result_data.get("traceback")
                execution.completed_at = datetime.utcnow()
                await db.commit()
                raise FunctionExecutionError(result_data.get("error", "Unknown error"))

            # Completed
            output = result_data.get("result")
            duration_ms = result_data.get("duration_ms", 0)

            execution.status = ExecutionStatus.COMPLETED
            execution.output_data = output
            execution.completed_at = datetime.utcnow()
            execution.duration_ms = duration_ms
            execution.container_id = None

            await db.commit()

            await clickhouse_logger.log_execution_end(
                execution_id, "completed", output, None, duration_ms
            )

            return output

    async def enqueue_function(
        self,
        function_namespace: str,
        function_name: str,
        input_data: dict[str, Any],
        execution_id: str,
        trigger_type: str,
        trigger_id: str,
        user_id: str,
        chat_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Enqueue a function for execution via the job queue.

        Creates an Execution record with PENDING status and dispatches to the queue.
        Returns immediately with job_id and execution_id.
        """
        from app.services.queue_service import queue_service

        # Create Execution record with PENDING status
        async with AsyncSessionLocal() as db:
            execution = Execution(
                user_id=user_id,
                execution_id=execution_id,
                function_name=function_name,
                trigger_type=trigger_type,
                trigger_id=trigger_id,
                chat_id=chat_id,
                status=ExecutionStatus.PENDING,
                input_data=input_data,
            )
            db.add(execution)
            await db.commit()

        # Enqueue via queue service
        job_id = await queue_service.enqueue_function(
            function_namespace=function_namespace,
            function_name=function_name,
            input_data=input_data,
            execution_id=execution_id,
            trigger_type=trigger_type,
            trigger_id=trigger_id,
            user_id=user_id,
            chat_id=chat_id,
        )

        return {
            "status": "queued",
            "execution_id": execution_id,
            "job_id": job_id,
        }

    def clear_cache(self):
        """Clear function cache."""
        self.functions_cache.clear()


# Global executor instance
executor = FunctionExecutor()
