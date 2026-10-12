"""An in-process MCP server that speaks the Tasks extension (2026-07-28).

Used by the MCP tasks tests. Tools:
- `quick`: an ordinary tool, always answered inline;
- `slow`: answered with a task that completes after `complete_after` polls;
- `ask`: a task that waits for input (`confirm`) before it completes;
- `boom`: a task that fails;
- `forever`: a task that never finishes (until cancelled).

A task is only returned to a client that declared the extension on that
request, as the spec requires.
"""
import contextlib
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx2
from mcp.client.streamable_http import streamable_http_client
from mcp.server.extension import Extension, MethodBinding
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import RequestParams
from pydantic import ConfigDict

TASKS = "io.modelcontextprotocol/tasks"
TASK_TOOLS = {"slow", "ask", "boom", "forever"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text_result(text: str) -> dict[str, Any]:
    # What the tool (`-> str`, so an output schema of {result: string}) would
    # have returned inline.
    return {"content": [{"type": "text", "text": text}], "structuredContent": {"result": text}, "isError": False}


class _TaskIdParams(RequestParams):
    model_config = ConfigDict(extra="allow")
    task_id: str


class _UpdateParams(RequestParams):
    model_config = ConfigDict(extra="allow")
    task_id: str
    input_responses: dict[str, Any]


class TasksExtension(Extension):
    identifier = TASKS

    def __init__(self, complete_after: int = 2, poll_interval_ms: int = 10):
        self.complete_after = complete_after
        self.poll_interval_ms = poll_interval_ms
        self.tasks: dict[str, dict[str, Any]] = {}
        self.declared: list[bool] = []  # per tools/call: did the client declare Tasks?
        self.cancelled: list[str] = []
        self.updates: list[dict[str, Any]] = []

    def _task(self, task_id: str) -> dict[str, Any]:
        t = self.tasks[task_id]
        out = {
            "taskId": task_id, "status": t["status"], "createdAt": t["created"], "lastUpdatedAt": _now(),
            "ttlMs": 60_000, "pollIntervalMs": self.poll_interval_ms,
        }
        if t.get("message"):
            out["statusMessage"] = t["message"]
        return out

    async def intercept_tool_call(self, params, ctx, call_next):
        meta = ctx.meta or {}
        if not isinstance(meta, dict):
            meta = meta.model_dump(by_alias=True)
        caps = meta.get("io.modelcontextprotocol/clientCapabilities") or {}
        declared = TASKS in (caps.get("extensions") or {})
        self.declared.append(declared)
        if params.name not in TASK_TOOLS or not declared:
            return await call_next(ctx)
        task_id = uuid.uuid4().hex
        self.tasks[task_id] = {"tool": params.name, "status": "working", "polls": 0, "created": _now(),
                               "arguments": dict(params.arguments or {})}
        return {**self._task(task_id), "resultType": "task"}

    async def _get(self, ctx, params: _TaskIdParams):
        t = self.tasks.get(params.task_id)
        if t is None:
            from mcp.shared.exceptions import MCPError

            raise MCPError(code=-32602, message="unknown task")
        if t["status"] == "working":
            t["polls"] += 1
            if t["tool"] == "slow" and t["polls"] >= self.complete_after:
                t["status"], t["result"] = "completed", _text_result(f"done: {t['arguments'].get('text', '')}")
            elif t["tool"] == "boom" and t["polls"] >= 1:
                t["status"], t["error"] = "failed", {"code": -32000, "message": "it broke"}
            elif t["tool"] == "ask" and not t.get("answered"):
                t["status"] = "input_required"
        out = {**self._task(params.task_id), "resultType": "complete"}
        if t["status"] == "input_required":
            out["inputRequests"] = {
                "confirm": {"method": "elicitation/create",
                            "params": {"mode": "form", "message": "Really do it?",
                                       "requestedSchema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}}}
            }
        if t["status"] == "completed":
            out["result"] = t["result"]
        if t["status"] == "failed":
            out["error"] = t["error"]
        return out

    async def _update(self, ctx, params: _UpdateParams):
        self.updates.append({"task_id": params.task_id, "responses": params.input_responses})
        t = self.tasks[params.task_id]
        if "confirm" in params.input_responses:
            t["answered"] = True
            t["status"], t["result"] = "completed", _text_result(f"confirmed: {params.input_responses['confirm']}")
        return {"resultType": "complete"}

    async def _cancel(self, ctx, params: _TaskIdParams):
        self.cancelled.append(params.task_id)
        t = self.tasks.get(params.task_id)
        if t is not None and t["status"] in ("working", "input_required"):
            t["status"], t["message"] = "cancelled", "cancelled by the client"
        return {"resultType": "complete"}

    def methods(self):
        return (
            MethodBinding(method="tasks/get", params_type=_TaskIdParams, handler=self._get),
            MethodBinding(method="tasks/update", params_type=_UpdateParams, handler=self._update),
            MethodBinding(method="tasks/cancel", params_type=_TaskIdParams, handler=self._cancel),
        )


class TasksMcp:
    """The server plus a transport for `mcp_client.open_transport`. Records
    the `Mcp-Name` header of every tasks/* request."""

    def __init__(self, **extension_args):
        self.tasks = TasksExtension(**extension_args)
        self.server = MCPServer("tasks-fake", extensions=[self.tasks])
        self.mcp_names: list[str] = []

        @self.server.tool(description="Answers at once")
        def quick(text: str = "") -> str:
            return f"quick: {text}"

        for name in TASK_TOOLS:
            def _tool(text: str = "") -> str:  # only reached by clients without Tasks
                return f"inline: {text}"

            self.server.tool(name=name, description=f"{name} (runs as a task)")(_tool)

        self.app = self.server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

    async def _recording_app(self, scope, receive, send):
        if scope["type"] == "http":
            for key, value in scope.get("headers", []):
                if key.lower() == b"mcp-name":
                    self.mcp_names.append(value.decode())
        await self.app(scope, receive, send)

    def open_transport(self, server, headers: dict[str, str]):
        fake = self

        @contextlib.asynccontextmanager
        async def _transport():
            http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=fake._recording_app), headers=headers)
            async with http:
                async with streamable_http_client(server.url, http_client=http) as streams:
                    yield streams

        return _transport()
