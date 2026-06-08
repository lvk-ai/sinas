"""SandboxExecutor backed by the legacy `container_pool`.

Thin adapter. Delegates straight to the existing `container_pool`
singleton. No behavior change — exists so the executor abstraction has
a working default while we land the rest of the refactor.

This impl will be replaced by `DockerEphemeralSandboxExecutor` in step 3
of the migration, when we drop the long-lived pool in favor of
`docker run --rm` per execution.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


class DockerPoolSandboxExecutor:
    """Adapts `app.services.container_pool.container_pool` to `SandboxExecutor`."""

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
        # Imported lazily so `from app.services.executor import ...` does
        # not pull the Docker SDK into modules that only need the
        # Protocol types.
        from app.services.container_pool import container_pool

        return await container_pool.execute_function(
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
