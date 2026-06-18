"""SandboxExecutor: one ephemeral container per execution, baked package image.

See ADR 2026-06-17-ephemeral-sandbox-baked-image.md. Untrusted code runs in a
fresh container created from the pre-baked sandbox image, used exactly once,
then removed. No pool, no reuse — so no cross-execution leak and none of the
pool / discovery / replenish machinery.

The container hardening and in-container IPC mirror `container_pool` exactly
(same `container_executor` sandbox mode, same request/trigger/result-file
protocol); only the lifecycle differs (create + run + remove instead of
acquire/release). Docker SDK calls are blocking, so they run via
`asyncio.to_thread`; the SDK is imported lazily.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket as _socket_mod
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.executor.base import ExecutionResult

logger = logging.getLogger(__name__)


class DockerEphemeralSandboxExecutor:
    """Per-execution ephemeral Docker sandbox. Implements SandboxExecutor."""

    def __init__(self) -> None:
        self._network_ready = False

    def _ensure_network(self, client) -> str:
        import docker

        name = settings.sandbox_network
        if self._network_ready:
            return name
        try:
            client.networks.get(name)
        except docker.errors.NotFound:
            client.networks.create(name, driver="bridge", labels={"sinas.type": "sandbox"})
        self._network_ready = True
        return name

    def _run_config(self, image: str, name: str, network: str) -> dict[str, Any]:
        # Mirrors container_pool._do_create_container hardening, minus the
        # unless-stopped restart policy (this container is single-use).
        return {
            "image": image,
            "name": name,
            "detach": True,
            "network": network,
            "mem_limit": f"{settings.max_function_memory}m",
            "nano_cpus": int(settings.max_function_cpu * 1_000_000_000),
            "cap_drop": ["ALL"],
            "cap_add": ["CHOWN", "SETUID", "SETGID"],
            "security_opt": ["no-new-privileges:true"],
            "pids_limit": 256,
            "extra_hosts": {"host.docker.internal": "host-gateway"},
            "tmpfs": {"/tmp": "size=100m,mode=1777"},
            "environment": {
                "PYTHONUNBUFFERED": "1",
                "SANDBOX_CONTAINER": "true",
                "SINAS_CONTAINER_MODE": "sandbox",
            },
            "labels": {"sinas.type": "sandbox-executor", "sinas.ephemeral": "true"},
        }

    def _create_container(self, client, config: dict[str, Any]):
        # Remove a stale container squatting on this name (e.g. a crashed prior
        # run of the same execution_id), then create. Apply the storage limit,
        # falling back if the storage driver doesn't support storage-opt.
        import docker

        try:
            stale = client.containers.get(config["name"])
            stale.remove(force=True)
        except docker.errors.NotFound:
            pass
        try:
            return client.containers.run(
                **config, storage_opt={"size": settings.max_function_storage}
            )
        except Exception as e:
            if "storage-opt" in str(e).lower() or "storage driver" in str(e).lower():
                return client.containers.run(**config)
            raise

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
    ) -> ExecutionResult:
        import docker

        from app.models.function import Function
        from app.services.sandbox_image import current_sandbox_image

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

        image = await current_sandbox_image(db)
        client = await asyncio.to_thread(docker.from_env)
        network = await asyncio.to_thread(self._ensure_network, client)
        name = f"sinas-sbx-{execution_id}"
        effective_timeout = timeout or settings.function_timeout

        config = self._run_config(image, name, network)
        container = await asyncio.to_thread(self._create_container, client, config)
        try:
            # Daemon warmup — match the pool's post-create settle.
            await asyncio.sleep(1)

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
                    "access_token": access_token,
                    "execution_id": execution_id,
                    "trigger_type": trigger_type,
                    "chat_id": chat_id,
                },
            }
            eid = execution_id
            request_path = f"/tmp/exec_request_{eid}.json"
            trigger_file = f"/tmp/exec_trigger_{eid}"
            result_file = f"/tmp/exec_result_{eid}.json"

            # Write the request via a stdin pipe (put_archive writes the overlay
            # layer, which is invisible under the tmpfs /tmp mount).
            payload_bytes = json.dumps(payload).encode("utf-8")
            api = container.client.api
            exec_id = await asyncio.to_thread(
                lambda: api.exec_create(
                    container.id,
                    [
                        "python3",
                        "-c",
                        f'import sys; open("{request_path}","wb").write(sys.stdin.buffer.read())',
                    ],
                    stdin=True,
                    stdout=True,
                    stderr=True,
                )["Id"]
            )

            def _pipe_request() -> None:
                sock = api.exec_start(exec_id, socket=True)
                sock._sock.sendall(payload_bytes)
                sock._sock.shutdown(_socket_mod.SHUT_WR)
                sock.read()
                sock.close()

            await asyncio.to_thread(_pipe_request)

            exec_result = await asyncio.to_thread(
                container.exec_run,
                cmd=[
                    "python3",
                    "-c",
                    f"""
import sys, json, time, os
with open("{trigger_file}", "w") as f:
    f.write("1")
max_wait = {effective_timeout}
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
print(json.dumps({{"error": "Execution timeout after {effective_timeout}s", "status": "failed"}}))
sys.exit(1)
""",
                ],
                demux=True,
            )

            stdout, stderr = exec_result.output
            stdout_str = stdout.decode() if stdout else ""
            if exec_result.exit_code != 0:
                stderr_str = stderr.decode() if stderr else ""
                return ExecutionResult.failed(
                    stderr_str or stdout_str or f"Exit code {exec_result.exit_code}"
                )
            return ExecutionResult.from_wire(json.loads(stdout_str))
        finally:
            try:
                await asyncio.to_thread(container.remove, force=True)
            except Exception as e:
                logger.warning("Failed to remove ephemeral sandbox %s: %s", name, e)

    async def resume(
        self,
        *,
        handle: str,
        resume_value: Any,
        execution_id: str,
        timeout: int,
    ) -> ExecutionResult:
        # Sandbox mode disables input() (it raises), and an ephemeral container
        # is gone after its single run — there is nothing to resume into. This
        # satisfies the Protocol; durable HITL is tracked in issue #79.
        return ExecutionResult.failed(
            "Ephemeral sandbox does not support resume (input() is disabled in sandbox mode)."
        )
