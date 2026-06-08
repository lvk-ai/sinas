"""TrustedExecutor backed by the legacy `shared_worker_manager`.

Thin adapter. Delegates straight to the existing `shared_worker_manager`
singleton. No behavior change.

This impl will be replaced by `InProcessTrustedExecutor` in step 4 of
the migration, at which point the dedicated shared-worker containers
are no longer needed for trusted code paths.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


class DockerSharedTrustedExecutor:
    """Adapts `app.services.shared_worker_manager.shared_worker_manager` to `TrustedExecutor`."""

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
        db: AsyncSession,
        timeout: int,
    ) -> dict[str, Any]:
        # Lazy import so the Docker SDK is not imported by callers that
        # only need the abstraction.
        from app.services.shared_worker_manager import shared_worker_manager

        return await shared_worker_manager.execute_function(
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
            timeout=timeout,
        )
