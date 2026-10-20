"""Skill-to-tool converter for LLM tool calling."""
import logging
from typing import Any, Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.skill import Skill

logger = logging.getLogger(__name__)


async def _skill_access(
    db: AsyncSession, user_id: Optional[str], permissions: Optional[dict[str, bool]]
) -> Callable[[Any], bool]:
    """Which skills a wildcard may reach: those the user may read."""
    from app.services.resource_resolver import user_can

    if user_id and permissions is None:
        from app.core.auth import get_effective_permissions

        permissions = await get_effective_permissions(db, user_id)
    return user_can(user_id, permissions, "read")


class SkillToolConverter:
    """Converts skills to OpenAI tool format and manages retrieval."""

    async def get_available_skills(
        self,
        db: AsyncSession,
        enabled_skills: Optional[list[dict[str, Any]]] = None,
        user_id: Optional[str] = None,
        permissions: Optional[dict[str, bool]] = None,
    ) -> list[dict[str, Any]]:
        """
        Get skills and convert to OpenAI tools format.

        Only skills with preload=false are exposed as tools.
        Preloaded skills (preload=true) should be retrieved via get_preloaded_skills_content().

        Args:
            db: Database session
            enabled_skills: List of skill configs with {"skill": "namespace/name", "preload": bool}
            user_id: The chat's user: a wildcard offers only skills they may read
            permissions: The user's effective permissions (looked up if None)

        Returns:
            List of skills in OpenAI tool format (only non-preloaded)
        """
        tools = []

        # If no enabled_skills specified, return empty
        if not enabled_skills:
            return tools

        from app.services.resource_resolver import resolve_resource_refs

        # Separate preloaded from on-demand, expand wildcards for on-demand
        on_demand_refs = []
        for skill_config in enabled_skills:
            if skill_config.get("preload", False):
                continue
            skill_ref = skill_config.get("skill", "")
            if skill_ref:
                on_demand_refs.append(skill_ref)

        resolved = await resolve_resource_refs(
            db, on_demand_refs, Skill, can_access=await _skill_access(db, user_id, permissions)
        )

        for skill_ref, skill in resolved:

            # Convert skill to tool format
            tool = self.skill_to_tool(skill)
            tools.append(tool)

        return tools

    async def get_preloaded_skills_content(
        self,
        db: AsyncSession,
        enabled_skills: Optional[list[dict[str, Any]]] = None,
        user_id: Optional[str] = None,
    ) -> str:
        """
        Get content for preloaded skills to inject into system prompt.

        Only returns content for skills with preload=true.

        Args:
            db: Database session
            enabled_skills: List of skill configs with {"skill": "namespace/name", "preload": bool}
            user_id: The chat's user: a wildcard preloads only skills they may read

        Returns:
            Combined markdown content for all preloaded skills
        """
        contents = []

        # If no enabled_skills specified, return empty
        if not enabled_skills:
            return ""

        from app.services.resource_resolver import resolve_resource_refs

        preload_refs = []
        for skill_config in enabled_skills:
            if not skill_config.get("preload", False):
                continue
            skill_ref = skill_config.get("skill", "")
            if skill_ref:
                preload_refs.append(skill_ref)

        resolved = await resolve_resource_refs(
            db, preload_refs, Skill, can_access=await _skill_access(db, user_id, None)
        )

        for skill_ref, skill in resolved:
            contents.append(f"# Skill: {skill.namespace}/{skill.name}\n\n{skill.content}")

        return "\n\n---\n\n".join(contents)

    def skill_to_tool(self, skill: Skill) -> dict[str, Any]:
        """
        Convert a skill to OpenAI tool format.

        The skill's description becomes the tool description (shown to LLM),
        and calling the tool returns the skill's content (markdown instructions).

        Args:
            skill: Skill model instance

        Returns:
            Tool definition in OpenAI format
        """
        # Create a safe function name from namespace/name
        # Replace non-alphanumeric chars with underscore
        safe_name = f"get_skill_{skill.namespace}_{skill.name}".replace("-", "_")

        return {
            "type": "function",
            "function": {
                "name": safe_name,
                "description": skill.description,
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }

    async def handle_skill_tool_call(
        self,
        db: AsyncSession,
        tool_name: str,
        arguments: dict[str, Any],
        enabled_skills: Optional[list[Any]] = None,
        user_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Handle a skill tool call by returning the skill's content.

        Args:
            db: Database session
            tool_name: Name of the tool (e.g., "get_skill_default_web_research")
            arguments: Tool arguments (empty for skills)
            enabled_skills: The chat agent's enabled skills: when given, only a
                skill they grant (a wildcard: one the user may read) is returned
            user_id: The chat's user

        Returns:
            Skill content (markdown) or None if skill not found
        """
        # Extract namespace/name from tool name
        # Format: get_skill_{namespace}_{name}
        if not tool_name.startswith("get_skill_"):
            logger.warning(f"Invalid skill tool name: {tool_name}")
            return None

        # Remove prefix and split
        skill_id = tool_name[len("get_skill_") :]
        parts = skill_id.split("_", 1)

        if len(parts) != 2:
            logger.warning(f"Could not parse skill from tool name: {tool_name}")
            return None

        namespace, name = parts
        # Convert back from safe name (underscores to hyphens)
        namespace = namespace.replace("_", "-")
        name = name.replace("_", "-")

        # Load skill
        skill = await Skill.get_by_name(db, namespace, name)

        if not skill or not skill.is_active:
            logger.warning(f"Skill {namespace}/{name} not found or inactive")
            return None

        if enabled_skills is not None:
            from app.services.resource_resolver import ref_granted

            ref = f"{skill.namespace}/{skill.name}"
            if not ref_granted(ref, enabled_skills, skill, await _skill_access(db, user_id, None), "skill"):
                logger.warning(f"Security: skill '{ref}' is not enabled for this agent")
                return None

        # Return the skill content (markdown instructions)
        return skill.content
