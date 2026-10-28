"""Component-to-tool converter for LLM tool calling.

When an agent has `enabled_components`, each component is exposed as a tool
the LLM can call. Calling the tool returns a component reference block that
the frontend renders as an embedded iframe.
"""
import json
import logging
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.component import Component

logger = logging.getLogger(__name__)


async def _component_access(
    db: AsyncSession, user_id: Optional[str], permissions: Optional[dict[str, bool]]
) -> Callable[[Any], bool]:
    """Which components a wildcard may reach: those the user may read (the
    components API's check: own, shared or public, or read:all)."""
    from app.services.resource_resolver import user_can

    if user_id and permissions is None:
        from app.core.auth import get_effective_permissions

        permissions = await get_effective_permissions(db, user_id)
    return user_can(user_id, permissions, "read")


class ComponentToolConverter:
    """Converts components to OpenAI tool format for agent tool calling."""

    async def get_available_components(
        self,
        db: AsyncSession,
        enabled_components: Optional[list[str]] = None,
        user_id: Optional[str] = None,
        permissions: Optional[dict[str, bool]] = None,
    ) -> list[dict[str, Any]]:
        """
        Get components and convert to OpenAI tools format.

        Args:
            db: Database session
            enabled_components: "namespace/name" component references, or
                "namespace/*" / "*/*" wildcards
            user_id: The chat's user: a wildcard offers only components they may read
            permissions: The user's effective permissions (looked up if None)

        Returns:
            List of components in OpenAI tool format
        """
        tools = []

        if not enabled_components:
            return tools

        from app.services.resource_resolver import resolve_resource_refs

        resolved = await resolve_resource_refs(
            db, enabled_components, Component,
            can_access=await _component_access(db, user_id, permissions),
        )
        for _, component in resolved:
            if not component.is_active:
                continue
            tools.append(self._component_to_tool(component))

        return tools

    def _component_to_tool(self, component: Component) -> dict[str, Any]:
        """
        Convert a component to OpenAI tool format.

        The component's input_schema defines the tool parameters.
        When called, returns a component reference block for frontend rendering.
        """
        # "{ns}__{name}", as functions and queries name their tools (and as
        # tool_name_to_status_key reads them back). Replacing "-" with "_"
        # and splitting on the first "_" lost any name containing either.
        safe_name = f"show_component_{component.namespace}__{component.name}"

        description = (
            component.description
            or f"Show the '{component.title or component.name}' interactive component"
        )

        # Use component's input_schema as tool parameters, or empty object
        parameters = component.input_schema or {
            "type": "object",
            "properties": {},
            "required": [],
        }

        return {
            "type": "function",
            "function": {
                "name": safe_name,
                "description": description,
                "parameters": parameters,
            },
        }

    async def _exact_lookup(self, db: AsyncSession, comp_id: str) -> tuple[Optional[Component], bool]:
        """The component whose tool name is exactly this. A namespace or
        name may itself contain "__", so every split is tried; two different
        components producing the same name is refused, not guessed."""
        matches = []
        start = 0
        while (index := comp_id.find("__", start)) != -1:
            namespace, name = comp_id[:index], comp_id[index + 2:]
            if namespace and name:
                component = await Component.get_by_name(db, namespace, name)
                if component is not None and component.is_active:
                    matches.append(component)
            start = index + 1
        if len(matches) > 1:
            logger.warning(f"Ambiguous component tool name: show_component_{comp_id}")
            return None, True
        return (matches[0] if matches else None), False

    async def _legacy_lookup(self, db: AsyncSession, comp_id: str) -> Optional[Component]:
        """Tool names recorded before the "__" form. First what the old
        handler looked up (split on the first "_", every "_" in the name
        read as "-"), so an old chat opens what it always opened; then the
        other spellings that produce this name, if exactly one exists."""
        namespace, _, name = comp_id.partition("_")
        if namespace and name:
            component = await Component.get_by_name(db, namespace, name.replace("_", "-"))
            if component is not None and component.is_active:
                return component
        candidates = (
            await db.execute(select(Component).where(Component.is_active == True))  # noqa: E712
        ).scalars().all()
        matches = [
            c for c in candidates
            if f"{c.namespace}_{c.name}".replace("-", "_") == comp_id
        ]
        if len(matches) > 1:
            logger.warning(f"Ambiguous legacy component tool name: show_component_{comp_id}")
            return None
        return matches[0] if matches else None

    async def handle_component_tool_call(
        self,
        db: AsyncSession,
        tool_name: str,
        arguments: dict[str, Any],
        user_id: str = "",
        enabled_components: Optional[list[str]] = None,
    ) -> Optional[dict[str, Any]]:
        """
        Handle a component tool call by returning a component reference block.

        The frontend detects these blocks in assistant messages and renders
        the component as an embedded iframe.

        Args:
            db: Database session
            tool_name: Name of the tool (e.g., "show_component_default_dashboard")
            arguments: Tool arguments (component input vars)
            user_id: User ID for generating render token
            enabled_components: The chat agent's enabled components: when
                given, only a component they grant is shown (through a
                wildcard: only one the user may read, as discovery offers)

        Returns:
            Component reference dict or None if component not found
        """
        if not tool_name.startswith("show_component_"):
            logger.warning(f"Invalid component tool name: {tool_name}")
            return None

        comp_id = tool_name[len("show_component_"):]
        component, ambiguous = (
            await self._exact_lookup(db, comp_id) if "__" in comp_id else (None, False)
        )
        if ambiguous:
            return None  # refused, not "not found": no fallback guess
        if component is None:
            # Tool calls recorded before the "__" form (in existing chats):
            # match the old spelling, where "-" had become "_" — which can
            # itself contain "__" (a name like "a--b").
            component = await self._legacy_lookup(db, comp_id)
        if component is None or not component.is_active:
            logger.warning(f"Could not resolve component from tool name: {tool_name}")
            return None
        namespace, name = component.namespace, component.name
        if not component or not component.is_active:
            logger.warning(f"Component {namespace}/{name} not found or inactive")
            return None
        if enabled_components is not None:
            from app.services.resource_resolver import ref_granted

            access = await _component_access(db, user_id, None)
            if not ref_granted(f"{namespace}/{name}", enabled_components, component, access):
                logger.warning(f"Security: component '{namespace}/{name}' is not enabled for this agent")
                return None

        # Generate render token for iframe embedding
        from app.services.content_tokens import generate_component_render_token

        render_token = generate_component_render_token(
            component.namespace, component.name, user_id
        )

        # Return component reference block (rendered client-side in iframe)
        display_name = component.title or f"{component.namespace}/{component.name}"
        return {
            "type": "component",
            "namespace": component.namespace,
            "name": component.name,
            "title": display_name,
            "input": arguments,
            "render_token": render_token,
            "display": f"[USER WILL SEE COMPONENT '{display_name}' HERE]",
        }
