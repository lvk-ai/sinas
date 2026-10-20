"""Connector tool converter — exposes connector operations as agent tools."""
import logging
import re
from typing import Any, Callable, Optional

from jinja2 import Template
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connector import Connector
from app.services.connector_service import connector_service

logger = logging.getLogger(__name__)

# Prefix for all connector tool names
CONNECTOR_TOOL_PREFIX = "connector__"


async def _connector_access(
    db: AsyncSession, user_id: Optional[str], permissions: Optional[dict[str, bool]]
) -> Callable[[Any], bool]:
    """Which connectors a wildcard may reach: those the user may read, the
    access the connectors API requires to run an operation."""
    from app.services.resource_resolver import user_can

    if user_id and permissions is None:
        from app.core.auth import get_effective_permissions

        permissions = await get_effective_permissions(db, user_id)
    return user_can(user_id, permissions, "read")


def connector_binding(
    connector_ref: str, enabled_connectors: list[Any]
) -> Optional[tuple[Optional[set[str]], dict[str, Any]]]:
    """What the entries enable on this connector: (operation names, or None
    for all of them; parameters by operation), or None when no entry
    matches. The most specific entries decide (an exact entry over
    "namespace/*" over "*/*"), so an exact entry narrows or widens what a
    wildcard gives one connector; equally specific entries combine: the
    union of their operations, and per operation the first entry's parameters."""
    from app.services.resource_resolver import matching_entries

    _, entries = matching_entries(connector_ref, enabled_connectors, "connector")
    if not entries:
        return None
    operations: Optional[set[str]] = set()
    parameters: dict[str, Any] = {}
    for entry in entries:
        entry = entry if isinstance(entry, dict) else {}
        entry_ops = entry.get("operations") or []
        if not entry_ops:
            operations = None  # every operation
        elif operations is not None:
            operations.update(entry_ops)
        for op_name, op_params in (entry.get("parameters") or {}).items():
            if op_name not in parameters and (not entry_ops or op_name in entry_ops):
                parameters[op_name] = op_params
    return operations, parameters


class ConnectorToolConverter:
    """Converts connector operations to OpenAI-format tools and executes them."""

    async def get_available_connectors(
        self,
        db: AsyncSession,
        enabled_connectors: Optional[list[dict[str, Any]]] = None,
        agent_input_context: Optional[dict[str, Any]] = None,
        user_id: Optional[str] = None,
        permissions: Optional[dict[str, bool]] = None,
    ) -> list[dict[str, Any]]:
        """Build tool definitions for enabled connector operations.

        enabled_connectors format:
        [
            {
                "connector": "namespace/name",   # or "namespace/*", "*/*"
                "operations": ["post_message", "get_channel"],
                "parameters": {
                    "post_message": {"channel": "{{ slack_channel }}"}
                }
            }
        ]

        A wildcard entry's operations and parameters apply to every connector
        it matches; see `connector_binding` for several entries matching one.
        """
        if not enabled_connectors:
            return []

        from app.services.resource_resolver import entry_ref, resolve_resource_refs

        # "namespace/*" and "*/*" match every connector there that the user
        # may use themselves (read access, as the connectors API requires to
        # run an operation); an exact entry names one, as it always did.
        resolved = await resolve_resource_refs(
            db,
            [entry_ref(entry, "connector") for entry in enabled_connectors],
            Connector,
            can_access=await _connector_access(db, user_id, permissions),
        )

        tools = []
        for connector_ref, connector in resolved:
            if not connector.is_active:
                logger.warning(f"Connector '{connector_ref}' is inactive")
                continue

            binding = connector_binding(connector_ref, enabled_connectors)
            if binding is None:
                continue
            enabled_ops, param_overrides = binding

            for op in connector.operations:
                op_name = op.get("name")
                if not op_name:
                    continue
                # Filter to enabled operations
                if enabled_ops is not None and op_name not in enabled_ops:
                    continue

                # Resolve parameter overrides
                locked_params = {}
                op_params = param_overrides.get(op_name, {})
                if op_params and agent_input_context:
                    for param_key, param_value in op_params.items():
                        if isinstance(param_value, str) and "{{" in param_value:
                            try:
                                locked_params[param_key] = Template(param_value).render(**agent_input_context)
                            except Exception:
                                locked_params[param_key] = param_value
                        else:
                            locked_params[param_key] = param_value
                elif op_params:
                    locked_params = dict(op_params)

                tool = self._operation_to_tool(connector, op, locked_params)
                tools.append(tool)

        return tools

    def _operation_to_tool(
        self,
        connector: Connector,
        operation: dict[str, Any],
        locked_params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Convert a connector operation to an OpenAI-format tool."""
        op_name = operation["name"]
        tool_name = f"{CONNECTOR_TOOL_PREFIX}{connector.namespace}__{connector.name}__{op_name}"

        # Build description
        desc = operation.get("description") or f"{operation['method']} {operation['path']}"
        desc = f"[{connector.namespace}/{connector.name}] {desc}"

        # Build parameters schema, excluding locked params
        parameters = dict(operation.get("parameters", {"type": "object", "properties": {}}))
        if locked_params and "properties" in parameters:
            filtered_props = {
                k: v for k, v in parameters["properties"].items()
                if k not in locked_params
            }
            parameters = {**parameters, "properties": filtered_props}
            # Remove locked params from required
            if "required" in parameters:
                parameters["required"] = [
                    r for r in parameters["required"]
                    if r not in locked_params
                ]
                if not parameters["required"]:
                    del parameters["required"]

        return {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": desc,
                "parameters": parameters,
                "_metadata": {
                    "type": "connector",
                    "connector_namespace": connector.namespace,
                    "connector_name": connector.name,
                    "operation_name": op_name,
                    "locked_params": locked_params or {},
                },
            },
        }

    async def execute_connector_tool(
        self,
        db: AsyncSession,
        tool_name: str,
        arguments: dict[str, Any],
        user_token: Optional[str] = None,
        locked_params: Optional[dict[str, Any]] = None,
        user_id: Optional[str] = None,
        enabled_connectors: Optional[list[Any]] = None,
    ) -> dict[str, Any]:
        """Execute a connector tool call.

        With `enabled_connectors` (the chat agent's, as of now), the call must
        still be within them, resolved as discovery resolves them: a
        connector reached through a wildcard only when the user may read it,
        and only an operation the deciding entry enables."""
        # Parse tool name: connector__namespace__name__operation
        parts = tool_name[len(CONNECTOR_TOOL_PREFIX):].split("__", 2)
        if len(parts) != 3:
            return {"error": f"Invalid connector tool name: {tool_name}"}

        namespace, name, operation_name = parts

        connector = await Connector.get_by_name(db, namespace, name)
        if not connector or not connector.is_active:
            return {"error": f"Connector '{namespace}/{name}' not found or inactive"}

        if enabled_connectors is not None:
            from app.services.resource_resolver import ref_granted

            ref = f"{namespace}/{name}"
            access = await _connector_access(db, user_id, None)
            binding = connector_binding(ref, enabled_connectors)
            if binding is None or not ref_granted(ref, enabled_connectors, connector, access, "connector"):
                logger.warning(f"Security: connector '{ref}' is not enabled for this agent")
                return {"error": f"Connector '{ref}' is not enabled for this agent"}
            operations, _ = binding
            if operations is not None and operation_name not in operations:
                return {"error": f"Operation '{operation_name}' of connector '{ref}' is not enabled for this agent"}

        # Merge locked params with LLM-provided arguments
        merged_params = {**(locked_params or {}), **arguments}

        try:
            print(f"🔌 Executing connector: {namespace}/{name}/{operation_name}")
            result = await connector_service.execute_operation(
                db=db,
                connector=connector,
                operation_name=operation_name,
                parameters=merged_params,
                user_token=user_token,
                user_id=user_id,
            )
            logger.info(f"Connector tool result: {namespace}/{name}/{operation_name} -> status={result.get('status_code')}")
            return result
        except Exception as e:
            logger.error(f"Connector operation failed: {namespace}/{name}/{operation_name}: {e}", exc_info=True)
            return {"error": str(e)}


def parse_connector_tool_name(tool_name: str) -> Optional[tuple[str, str, str]]:
    """Parse connector tool name into (namespace, name, operation).
    Returns None if not a connector tool.
    """
    if not tool_name.startswith(CONNECTOR_TOOL_PREFIX):
        return None
    parts = tool_name[len(CONNECTOR_TOOL_PREFIX):].split("__", 2)
    if len(parts) != 3:
        return None
    return tuple(parts)
