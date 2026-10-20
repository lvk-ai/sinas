"""MCP tool converter — exposes a remote MCP server's tools as agent tools.

Mirrors ConnectorToolConverter: discovery builds OpenAI-format tool
definitions (with `_metadata.tool_type = "mcp"`), execution dispatches on
that metadata. Filtering happens twice: the server's own allow/deny lists
(what the resource exposes at all) and the agent binding's `tools` globs
(what this agent gets).
"""
from __future__ import annotations

import fnmatch
import hashlib
import logging
import re
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_effective_permissions
from app.core.config import settings
from app.models.mcp_server import McpServer
from app.services import mcp_client
from app.services.mcp_client import MCP_TOOL_PREFIX, ListedTool, McpClientError

logger = logging.getLogger(__name__)


def server_allows(server: McpServer, tool_name: str) -> bool:
    """The resource-level filter: deny wins, an empty allow list means all."""
    if any(fnmatch.fnmatchcase(tool_name, pattern) for pattern in (server.tool_deny or [])):
        return False
    allow = server.tool_allow or []
    return not allow or any(fnmatch.fnmatchcase(tool_name, pattern) for pattern in allow)


def binding_allows(patterns: list[str], tool_name: str) -> bool:
    """The agent-level filter: empty means every tool the server allows."""
    return not patterns or any(fnmatch.fnmatchcase(tool_name, pattern) for pattern in patterns)


def _sanitize(name: str) -> str:
    # OpenAI function names allow [A-Za-z0-9_-]; MCP tool names may not.
    return re.sub(r"[^A-Za-z0-9_-]", "_", name) or "tool"


def parse_mcp_tool_name(tool_name: str) -> Optional[tuple[str, str, str]]:
    """(namespace, server, sanitized tool) from an mcp_ tool name, or None.
    The real MCP tool name lives in `_metadata.mcp_tool`; this is only a
    fallback for callers without metadata."""
    if not tool_name.startswith(MCP_TOOL_PREFIX):
        return None
    parts = tool_name[len(MCP_TOOL_PREFIX):].split("__", 2)
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


async def accessible_server(
    db: AsyncSession,
    namespace: str,
    name: str,
    user_id: Optional[str],
    permissions: Optional[dict[str, bool]] = None,
) -> tuple[Optional[McpServer], Optional[str]]:
    """The server, or why the caller may not use it. Access follows the
    resource's own permission model (sinas.mcp_servers/<ns>/<name>.read, :own
    for the owner, :all for everyone else) — the same check the REST API
    applies — so binding a server to an agent never grants the chat's user a
    server, or its credentials, they could not read themselves. The
    permissions are the EFFECTIVE ones: a run acting through an API key is
    held to the key's grants, not the owner's."""
    server = await McpServer.get_by_name(db, namespace, name)
    if not server or not server.is_active:
        return None, f"MCP server '{namespace}/{name}' not found or inactive"
    if not user_id:
        return None, f"MCP server '{namespace}/{name}': no user to authorize"
    if permissions is None:
        permissions = await get_effective_permissions(db, user_id)
    if not server.can_user_access(str(user_id), permissions, "read"):
        return None, f"MCP server '{namespace}/{name}': not authorized"
    return server, None


async def _current_binding_problem(
    db: AsyncSession, chat: Any, namespace: str, name: str, mcp_tool: str
) -> Optional[str]:
    """Why the chat's agent may not call `mcp_tool` on `namespace/name` as
    of now, or None. No chat (a pipeline or direct call) means no agent
    binding to check — those paths carry their own access checks."""
    if chat is None or not getattr(chat, "agent_id", None):
        return None
    from app.models.agent import Agent

    agent = (await db.execute(select(Agent).where(Agent.id == chat.agent_id))).scalar_one_or_none()
    if agent is None:
        return None
    ref = f"{namespace}/{name}"
    # Discovery unions the deciding bindings for a server; so does this
    # check — a tool one binding allows must not be refused because another
    # lists it under a different pattern. (Access to the server itself, the
    # boundary a wildcard binding relies on, is checked by the caller.)
    bindings = _deciding_bindings(ref, agent.enabled_mcp_servers or [])
    if not bindings:
        return f"MCP server '{ref}' is no longer enabled for this agent"
    if any(binding_allows(patterns, mcp_tool) for patterns in bindings):
        return None
    return f"Tool '{mcp_tool}' on MCP server '{ref}' is outside this agent's tool patterns"


def _deciding_bindings(ref: str, entries: list[Any]) -> list[list[str]]:
    """The tool patterns of the bindings that decide what the agent gets
    from server `ref`: the most specific ones matching it (an exact binding
    over "namespace/*" over "*/*", so an exact binding narrows or widens
    what a wildcard gives one server). Several equally specific bindings
    are one binding with the union of their patterns."""
    from app.services.resource_resolver import matching_entries

    _, matched = matching_entries(ref, entries, "server")
    return [_normalize_entry(entry)[1] for entry in matched]


def _normalize_entry(entry: Any) -> tuple[str, list[str]]:
    if isinstance(entry, str):
        return entry, []
    if isinstance(entry, dict):
        return str(entry.get("server", "")), list(entry.get("tools") or [])
    return str(getattr(entry, "server", "")), list(getattr(entry, "tools", None) or [])


class McpToolConverter:
    """Converts a server's MCP tools to OpenAI-format tools and executes them."""

    async def get_available_tools(
        self,
        db: AsyncSession,
        enabled_mcp_servers: Optional[list[Any]],
        user_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Tool definitions for every enabled server that answers. A server
        that is missing, inactive or unreachable contributes no tools and a
        log line — never a failed chat turn."""
        permissions: Optional[dict[str, bool]] = None
        # Names must be unique across ALL servers, not per server: the
        # dispatcher looks a call up by name, and 'a__b/c' and 'a/b__c' both
        # encode a tool 'echo' as mcp_a__b__c__echo. Candidates are gathered
        # first so a collision renames EVERY party to it, independent of
        # discovery order: an approval rule on a tool's name must keep
        # matching the same remote operation whatever the listing order.
        candidates: list[tuple[McpServer, ListedTool]] = []
        # A server bound more than once is one server with the union of its
        # bindings' patterns: each remote tool is offered once (an approval
        # rule matching its name then covers every call to it), and the
        # names never depend on how many bindings there are.
        # A wildcard binding ("namespace/*", "*/*") binds every active server
        # it matches that the user may read (the access check below applies
        # to every server alike); its tool patterns apply to each.
        from app.services.resource_resolver import EXACT, ref_specificity, resolve_resource_refs, user_can

        entries = list(enabled_mcp_servers or [])
        refs: list[str] = []
        for entry in entries:
            ref, _ = _normalize_entry(entry)
            if ref_specificity(ref) in (None, EXACT):
                refs.append(ref)  # exact (or invalid: reported below), as always
                continue
            if permissions is None and user_id:
                permissions = await get_effective_permissions(db, user_id)
            matched = await resolve_resource_refs(
                db, [ref], McpServer, can_access=user_can(user_id, permissions, "read")
            )
            refs += [matched_ref for matched_ref, _ in matched]
        combined: dict[str, list[list[str]]] = {}
        for ref in refs:
            if ref not in combined:
                combined[ref] = _deciding_bindings(ref, entries)
        for ref, bindings in combined.items():
            parts = ref.split("/", 1)
            if len(parts) != 2:
                logger.warning(f"Invalid MCP server reference: {ref!r}")
                continue
            namespace, name = parts
            if permissions is None and user_id:
                permissions = await get_effective_permissions(db, user_id)
            server, problem = await accessible_server(db, namespace, name, user_id, permissions)
            if server is None:
                logger.warning(f"MCP tools unavailable: {problem}")
                continue
            try:
                listed = await mcp_client.list_tools(db, server, user_id)
            except McpClientError as e:
                logger.warning(f"MCP tools unavailable: {e}")
                continue
            except Exception as e:  # pragma: no cover - belt and braces
                logger.warning(f"MCP tools unavailable for '{ref}': {e}", exc_info=True)
                continue
            for tool in listed:
                if not server_allows(server, tool.name):
                    continue
                if not any(binding_allows(patterns, tool.name) for patterns in bindings):
                    continue
                candidates.append((server, tool))
        counts: dict[str, int] = {}
        for server, tool in candidates:
            base = self._base_name(server, tool)
            counts[base] = counts.get(base, 0) + 1
        seen: set[str] = set()
        return [
            self._to_tool(server, tool, seen, collided=counts[self._base_name(server, tool)] > 1)
            for server, tool in candidates
        ]

    @staticmethod
    def _base_name(server: McpServer, tool: ListedTool) -> str:
        return f"{MCP_TOOL_PREFIX}{server.namespace}__{server.name}__{_sanitize(tool.name)}"

    def _to_tool(
        self, server: McpServer, tool: ListedTool, seen: set[str], *, collided: bool = False
    ) -> dict[str, Any]:
        base = self._base_name(server, tool)
        tool_name = base
        if collided or tool_name in seen:
            # Same encoded name from another server (or two MCP names that
            # sanitize alike): disambiguate with a digest of the server ref
            # and the raw tool name so the suffix is stable across turns —
            # and, with `collided`, applied to every party whatever the order.
            digest = hashlib.sha1(f"{server.namespace}/{server.name}:{tool.name}".encode()).hexdigest()[:6]
            tool_name = f"{base}_{digest}"
            n = 1
            while tool_name in seen:
                n += 1
                tool_name = f"{base}_{digest}_{n}"
        seen.add(tool_name)
        description = tool.description or tool.title or tool.name
        parameters = tool.input_schema or {"type": "object", "properties": {}}
        if parameters.get("type") != "object":
            parameters = {"type": "object", "properties": {}}
        metadata: dict[str, Any] = {
            "tool_type": "mcp",
            "server_namespace": server.namespace,
            "server_name": server.name,
            "mcp_tool": tool.name,
        }
        if tool.annotations:
            metadata["annotations"] = tool.annotations
        return {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": f"[{server.namespace}/{server.name}] {description}",
                "parameters": parameters,
                "_metadata": metadata,
            },
        }

    async def execute_tool(
        self,
        db: AsyncSession,
        tool_name: str,
        arguments: dict[str, Any],
        user_id: Optional[str],
        metadata: Optional[dict[str, Any]] = None,
        chat: Any = None,
        tool_call_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Call the tool and map its result. Errors are results, not raises."""
        metadata = metadata or {}
        namespace = metadata.get("server_namespace")
        name = metadata.get("server_name")
        mcp_tool = metadata.get("mcp_tool")
        if not (namespace and name and mcp_tool):
            parsed = parse_mcp_tool_name(tool_name)
            if not parsed:
                return {"error": f"Invalid MCP tool name: {tool_name}"}
            namespace, name, mcp_tool = parsed

        server, problem = await accessible_server(db, namespace, name, user_id)
        if server is None:
            return {"error": problem}
        if not server_allows(server, mcp_tool):
            return {"error": f"Tool '{mcp_tool}' is not allowed on MCP server '{namespace}/{name}'"}
        # The agent's CURRENT binding decides, not the tool list a resumed
        # round was saved with: a server unbound, or a pattern narrowed,
        # while a call waited for approval must not run anyway.
        binding_problem = await _current_binding_problem(db, chat, namespace, name, mcp_tool)
        if binding_problem:
            return {"error": binding_problem}

        store_blob = await self._blob_store(db, chat, user_id, tool_name) if chat is not None else None
        # A server may answer with a task (MCP Tasks): it is polled inline
        # for a short while, then kept and polled in the background with its
        # result delivered into the chat. Without a chat there is nowhere to
        # deliver to, so the call waits as long as the server's timeout.
        task_wait = (
            min(settings.mcp_task_inline_wait_seconds, server.timeout_seconds) if chat is not None
            else server.timeout_seconds
        )
        try:
            logger.info(f"🔌 Calling MCP tool: {namespace}/{name}/{mcp_tool}")
            result = await mcp_client.call_tool(
                db, server, mcp_tool, arguments or {}, user_id, task_wait_seconds=task_wait
            )
        except McpClientError as e:
            logger.warning(f"MCP call failed: {e}")
            return {"error": str(e)}
        except Exception as e:  # pragma: no cover
            logger.error(f"MCP call failed: {namespace}/{name}/{mcp_tool}: {e}", exc_info=True)
            return {"error": str(e)}
        if isinstance(result, mcp_client.TaskState):
            return await self._keep_task(db, chat, user_id, server, result, mcp_tool, tool_name, tool_call_id)
        return await mcp_client.map_call_result(
            result,
            tool_name=mcp_tool,
            store_blob=store_blob,
            blob_prefix=mcp_client.blob_prefix_for(tool_name, tool_call_id),
        )

    async def _keep_task(self, db, chat, user_id, server, task, mcp_tool, tool_name, tool_call_id) -> dict[str, Any]:
        from app.services import mcp_tasks

        if chat is None:
            # Nowhere to deliver a later result: give up on it, and say so.
            try:
                await mcp_client.cancel_task(db, server, user_id, task.task_id)
            except McpClientError:
                pass
            return {
                "error": f"MCP tool '{mcp_tool}' runs as a task that did not finish within "
                         f"{server.timeout_seconds}s (status: {task.status}); it needs a chat to "
                         f"report back to."
            }
        return await mcp_tasks.keep_task(
            db, chat=chat, user_id=str(user_id), server=server, task=task,
            tool_name=mcp_tool, model_tool_name=tool_name, tool_call_id=tool_call_id,
        )

    async def _blob_store(self, db: AsyncSession, chat: Any, user_id: Optional[str], tool_name: str):
        """Binary blocks go to the chat's workbench when the agent has one
        (the model gets a path, not a base64 wall); otherwise they inline."""
        from app.services.workbench import chat_has_workbench_enabled

        try:
            if chat is None or str(chat.user_id) != str(user_id):
                return None
            if not await chat_has_workbench_enabled(db, chat):
                return None
        except Exception as e:  # pragma: no cover
            logger.warning(f"Workbench check failed for MCP blob store: {e}")
            return None

        async def store(filename: str, content: bytes, mime_type: str) -> Optional[str]:
            from app.services.file_storage import get_storage
            from app.services.workbench import _write_bytes, get_or_create_workbench

            workbench = await get_or_create_workbench(db, chat)
            path = f"tool_results/{filename}"
            written = await _write_bytes(
                db,
                get_storage(),
                workbench,
                filename=path,
                content=content,
                content_type=mime_type,
                user_id=str(chat.user_id),
                visibility="private",
                file_metadata={"origin": "tool", "tool_name": tool_name},
            )
            if "error" in written:
                logger.warning(f"MCP blob write failed: {written['error']}")
                return None
            await db.commit()
            return path

        return store
