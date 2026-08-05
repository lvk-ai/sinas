"""Docker-based resume, shared by the two Docker executor adapters.

Lifted verbatim (behavior-wise) from the old
`execution_engine.resume_execution` so the structural refactor is a pure
move: write the resume value into the live container where the function
thread is parked on input(), then poll that container for the result.

This helper is DB-free — it does Docker I/O only and returns an
`ExecutionResult`. The engine owns all `Execution` persistence.

Both `DockerPoolSandboxExecutor` and `DockerSharedTrustedExecutor` delegate
here; the resume mechanism is identical for both (a Docker container the app
reaches via the shared daemon). `handle` is the container name/id.
"""

from __future__ import annotations

import asyncio
import json
import socket as _socket_mod
from typing import Any

from app.services.executor.base import ExecutionResult, ResultStatus


async def docker_resume(
    *,
    handle: str,
    resume_value: Any,
    execution_id: str,
    timeout: int,
) -> ExecutionResult:
    """Resume a function parked on input() inside Docker container `handle`."""
    import docker

    client = docker.from_env()
    try:
        container = client.containers.get(handle)
    except docker.errors.NotFound:
        # The live container is gone (recycle / deploy / crash). The parked
        # interpreter state went with it — the execution cannot be resumed.
        return ExecutionResult.failed("Container no longer available (restarted?)")

    # Write the resume value into the container via an exec stdin pipe.
    resume_payload = json.dumps({"value": resume_value}).encode("utf-8")
    resume_file = f"/tmp/exec_resume_{execution_id}.json"

    api = container.client.api
    exec_id = api.exec_create(
        container.id,
        [
            "python3",
            "-c",
            f'import sys; open("{resume_file}","wb").write(sys.stdin.buffer.read())',
        ],
        stdin=True,
        stdout=True,
        stderr=True,
    )["Id"]
    sock = api.exec_start(exec_id, socket=True)
    sock._sock.sendall(resume_payload)
    sock._sock.shutdown(_socket_mod.SHUT_WR)
    sock.read()
    sock.close()

    # Poll the container for the result the parked function produces.
    result_file = f"/tmp/exec_result_{execution_id}.json"
    exec_result = await asyncio.to_thread(
        container.exec_run,
        cmd=[
            "python3",
            "-c",
            f"""
import sys, json, time, os
max_wait = {timeout}
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
print(json.dumps({{"error": "Resume timeout after {timeout}s"}}))
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
        return ExecutionResult.failed(error_msg)

    res = ExecutionResult.from_wire(json.loads(stdout_str))

    # On a *subsequent* input() the container writes awaiting_input without a
    # container_name (it doesn't know its own name). Re-stamp the handle so the
    # next resume can find the same container.
    if res.status is ResultStatus.AWAITING_INPUT and not res.handle:
        from dataclasses import replace

        res = replace(res, handle=handle)

    return res
