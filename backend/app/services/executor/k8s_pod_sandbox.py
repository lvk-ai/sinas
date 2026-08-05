"""SandboxExecutor: one ephemeral Kubernetes Pod per execution.

Step 5 of the executor refactor — the k8s-native sibling of
`DockerEphemeralSandboxExecutor`. Untrusted code runs in a fresh hardened Pod
created from the executor image, used exactly once, then deleted. Pod
lifecycle and exec IPC live in `executor._k8s_runtime` and are shared with the
agent codeExecution path; this class supplies the function-execution payload.

Requires in-cluster ServiceAccount permissions: create/get/delete + exec on
pods in the sandbox namespace (the Helm chart ships the Role/RoleBinding).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.executor.base import ExecutionResult

logger = logging.getLogger(__name__)


class K8sPodSandboxExecutor:
    """Per-execution ephemeral k8s Pod. Implements SandboxExecutor."""

    async def execute(
        self,
        *,
        user_id: str,
        user_email: str,
        access_token: str,
        function_namespace: str,
        function_name: str,
        input_data: dict[str, Any],
        execution_id: str,
        trigger_type: str,
        chat_id: str | None,
        user_custom_fields: dict[str, Any] | None = None,
        db: AsyncSession,
        timeout: int,
    ) -> ExecutionResult:
        from app.models.function import Function
        from app.services.executor._k8s_runtime import (
            create_sandbox_pod,
            delete_sandbox_pod,
            run_payload_in_pod,
        )

        result = await db.execute(
            select(Function).where(
                Function.namespace == function_namespace,
                Function.name == function_name,
                Function.is_active == True,
            )
        )
        function = result.scalar_one_or_none()
        if not function:
            return ExecutionResult.failed(
                f"Function {function_namespace}/{function_name} not found"
            )

        effective_timeout = timeout or settings.function_timeout
        payload = {
            "action": "execute_inline",
            "function_code": function.code,
            "execution_id": execution_id,
            "function_namespace": function_namespace,
            "function_name": function_name,
            "timeout": effective_timeout,
            "input_data": input_data,
            "context": {
                "user_id": user_id,
                "user_email": user_email,
                "user_custom_fields": user_custom_fields or {},
                "access_token": access_token,
                "execution_id": execution_id,
                "trigger_type": trigger_type,
                "chat_id": chat_id,
            },
        }

        name, namespace = await create_sandbox_pod(db, execution_id=execution_id)
        try:
            wire = await run_payload_in_pod(
                name, namespace, payload, effective_timeout
            )
            return ExecutionResult.from_wire(wire)
        except Exception as e:
            logger.error("k8s sandbox execution %s failed: %s", execution_id, e)
            return ExecutionResult.failed(str(e))
        finally:
            await delete_sandbox_pod(name, namespace)

    async def resume(
        self,
        *,
        handle: str,
        resume_value: Any,
        execution_id: str,
        timeout: int,
    ) -> ExecutionResult:
        # Sandbox mode disables input() (it raises), and the pod is deleted
        # after its single run — nothing to resume into. Durable HITL for
        # sandboxed code is issue #79.
        return ExecutionResult.failed(
            "k8s pod sandbox does not support resume (input() is disabled in sandbox mode)."
        )
