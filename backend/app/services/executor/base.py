"""Executor Protocols.

Both `SandboxExecutor` and `TrustedExecutor` expose the same `execute`
shape — the call signature was historically identical between
`container_pool.execute_function` and `shared_worker_manager.execute_function`,
and we preserve that here. The two are kept as separate Protocols so that
mypy / Pyright catch accidental misuse: a function that requires sandbox
isolation cannot be silently routed to a trusted executor (and vice
versa), even though both happen to satisfy the same call shape today.

The result is still a `dict[str, Any]` for now, matching the current
contract callers in `execution_engine` rely on (`status`,
`awaiting_input`, `prompt`, `container_name`, etc.). A typed
`ExecutionResult` may follow once we have a stable list of fields; we
do not introduce one in this refactor to keep the diff a pure structural
move.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession


@runtime_checkable
class SandboxExecutor(Protocol):
    """Runs untrusted function code. Per-execution isolation required.

    Implementations must ensure that state written by one execution cannot
    leak to a subsequent execution (file system, env vars, network
    artifacts). Concrete impls today rely on Docker-level isolation; the
    k8s impl will rely on Pod-level isolation; future impls may rely on
    OS-level or VM-level sandboxing. The Protocol does not encode the
    mechanism, only the contract.
    """

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
    ) -> dict[str, Any]: ...


@runtime_checkable
class TrustedExecutor(Protocol):
    """Runs admin-approved function code (`Function.shared_pool=True`).

    No per-execution isolation requirement. Implementations may run code
    in-process, in a shared long-lived container, or any other shape that
    optimizes for throughput. Callers must only route code here when an
    admin has explicitly opted the function into the trusted pool.
    """

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
    ) -> dict[str, Any]: ...
