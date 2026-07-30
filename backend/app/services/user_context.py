"""
Per-user context exposed to agents, functions, and queries.

The same user context dict is injected as:
- the ``user`` template variable in agent system prompts and locked/overridable
  tool parameters ({{user.email}}, {{user.custom_fields.department}})
- the ``user_custom_fields`` entry in function execution context
- ``user_custom_<key>`` bind parameters in query execution

It is platform-provided data and always overrides caller-supplied template
variables of the same name, so locked parameters like
``{{user.custom_fields.region}}`` cannot be spoofed via agent input.
"""
import re
import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

# Bind parameter names must be valid SQL identifiers (see
# DatabasePoolManager._convert_params); skip custom field keys that aren't.
_SQL_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


async def load_user_context(db: AsyncSession, user_id: str) -> dict[str, Any]:
    """Load the user context dict: {"id", "email", "custom_fields"}."""
    result = await db.execute(select(User).where(User.id == uuid.UUID(str(user_id))))
    user = result.scalar_one_or_none()
    if not user:
        return {"id": str(user_id), "email": None, "custom_fields": {}}
    return {
        "id": str(user.id),
        "email": user.email,
        "custom_fields": user.custom_fields or {},
    }


def query_param_context(user_context: dict[str, Any]) -> dict[str, Any]:
    """
    Flatten a user context into SQL bind parameters:
    user_id, user_email, and user_custom_<key> for scalar custom fields.
    """
    params: dict[str, Any] = {"user_id": user_context["id"]}
    if user_context.get("email"):
        params["user_email"] = user_context["email"]

    for key, value in (user_context.get("custom_fields") or {}).items():
        if not _SQL_IDENTIFIER.match(key):
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            params[f"user_custom_{key}"] = value

    return params


def merge_user_template_context(
    template_variables: Optional[dict[str, Any]], user_context: dict[str, Any]
) -> dict[str, Any]:
    """Merge the user context into template variables under the ``user`` key.

    The platform-provided user context wins over any caller-supplied ``user``
    value — template variables come from chat metadata (agent input), which the
    end user controls, and locked parameters must not be spoofable.
    """
    return {**(template_variables or {}), "user": user_context}
