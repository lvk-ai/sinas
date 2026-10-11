"""Agents applier.

`is_active` is a soft delete: deleting an agent through the API switches it
off (recorded), and the API can switch it back on. Config and packages leave
that state alone unless they declare isActive. A package uninstall (or an
upgrade that drops one) does delete the row; its chats are kept, unlinked.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import select, update

from app.models.agent import Agent
from app.models.llm_provider import LLMProvider
from app.schemas.spec.agent import AgentSpec, McpBindingRef
from app.services.resources.base import ApplierError, ApplyContext, ReferenceNotFound, ResourceApplier

_FIELDS = (
    "description", "model", "max_tokens", "system_prompt", "icon", "default_job_timeout",
    "default_keep_alive", "is_active", "temperature",
)
_LISTS = (
    "enabled_functions", "enabled_agents", "enabled_queries", "enabled_components",
    "enabled_connectors", "enabled_pipelines", "system_tools",
)
_MAPS = ("input_schema", "output_schema", "function_parameters", "status_templates", "query_parameters")


class AgentApplier(ResourceApplier[AgentSpec]):
    kind = "agents"
    label = "Agent"
    noun = "agent"
    config_section = "agents"
    spec_model = AgentSpec
    model = Agent
    reference_fields = (
        "llm_provider_name", "enabled_functions", "enabled_skills", "enabled_queries",
        "enabled_stores", "enabled_collections", "enabled_components", "enabled_connectors",
        "enabled_pipelines", "enabled_agents", "hooks",
    )
    # Operator state: a deleted (switched-off) agent stays off, and the
    # default chosen in the console stays the default, unless config says so.
    singleton_lock = "default-agent"
    keep_unless_declared = ("is_active", "is_default")

    def key_of(self, spec: AgentSpec) -> str:
        return spec.key

    def key_of_row(self, row: Agent) -> str:
        return f"{row.namespace}/{row.name}"

    def config_key(self, item: Any) -> str:
        return f"{item.namespace}/{item.name}"

    async def find(self, ctx: ApplyContext, key: str) -> Agent | None:
        # Deleted (inactive) ones too: they still hold the name.
        namespace, name = key.split("/", 1)
        return (
            await ctx.db.execute(
                select(Agent)
                .where(Agent.namespace == namespace, Agent.name == name)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    def spec_from_row(self, row: Agent, provider_name: Optional[str] = None) -> AgentSpec:
        from app.schemas.spec.agent import CollectionRef, SkillRef, StoreRef

        return AgentSpec.model_construct(
            namespace=row.namespace,
            name=row.name,
            description=row.description or None,
            llm_provider_name=provider_name,
            model=row.model or None,
            provider_overrides=dict(row.provider_overrides) if row.provider_overrides else None,
            temperature=0.7 if row.temperature is None else row.temperature,
            max_tokens=row.max_tokens,
            system_prompt=row.system_prompt or None,
            input_schema=dict(row.input_schema or {}),
            output_schema=dict(row.output_schema or {}),
            initial_messages=list(row.initial_messages) if row.initial_messages is not None else None,
            enabled_functions=list(row.enabled_functions or []),
            function_parameters=dict(row.function_parameters or {}),
            status_templates=dict(row.status_templates or {}),
            enabled_agents=list(row.enabled_agents or []),
            # Older rows may hold plain strings: kept, with the meaning the
            # runtime and API give them (no preload, read-only), never dropped.
            enabled_skills=[
                SkillRef.model_construct(**({"preload": False, **s} if isinstance(s, dict) else {"skill": str(s), "preload": False}))
                for s in row.enabled_skills or []
            ],
            enabled_stores=[
                StoreRef.model_construct(**({"access": "readonly", **s} if isinstance(s, dict) else {"store": str(s), "access": "readonly"}))
                for s in row.enabled_stores or []
            ],
            enabled_queries=list(row.enabled_queries or []),
            query_parameters=dict(row.query_parameters or {}),
            enabled_collections=[
                CollectionRef.model_construct(**({"access": "readonly", **c} if isinstance(c, dict) else {"collection": str(c), "access": "readonly"}))
                for c in row.enabled_collections or []
            ],
            enabled_components=list(row.enabled_components or []),
            enabled_connectors=list(row.enabled_connectors or []),
            enabled_pipelines=list(row.enabled_pipelines or []),
            hooks=dict(row.hooks) if row.hooks else None,
            icon=row.icon or None,
            default_job_timeout=row.default_job_timeout,
            default_keep_alive=bool(row.default_keep_alive),
            system_tools=list(row.system_tools or []),
            tool_approvals=dict(row.tool_approvals) if row.tool_approvals else None,
            enabled_mcp_servers=[
                McpBindingRef.model_construct(**({"tools": [], **b} if isinstance(b, dict) else {"server": str(b), "tools": []}))
                for b in row.enabled_mcp_servers or []
            ],
            is_default=bool(row.is_default),
            is_active=row.is_active is not False,
        )

    async def current_spec(self, ctx: ApplyContext, row: Agent) -> AgentSpec:
        return self.spec_from_row(row, await provider_name(ctx.db, row.llm_provider_id))

    def new_row(self, spec: AgentSpec, ctx: ApplyContext) -> Agent:
        owner = ctx.owner_user_id
        return Agent(user_id=uuid.UUID(str(owner)) if owner else None)

    async def _provider_id(self, ctx: ApplyContext, name: str) -> uuid.UUID:
        provider_id = (
            await ctx.db.execute(select(LLMProvider.id).where(LLMProvider.name == name))
        ).scalar_one_or_none()
        if provider_id is None:
            raise ReferenceNotFound(f"LLM provider '{name}' not found")
        return provider_id

    async def write_row(
        self, row: Agent, spec: AgentSpec, ctx: ApplyContext, current: Optional[AgentSpec] = None
    ) -> None:
        # The provider is held by id; resolve the name only when the spec
        # names another provider than the one the change was computed against.
        if current is None or current.llm_provider_name != spec.llm_provider_name:
            row.llm_provider_id = (
                await self._provider_id(ctx, spec.llm_provider_name) if spec.llm_provider_name else None
            )
        # One default agent: making this one the default unsets the others,
        # each recorded (whoever manages them: the default is global).
        if spec.is_default and not (current is not None and current.is_default):
            await self._unset_other_defaults(row, ctx)
        row.namespace = spec.namespace
        row.name = spec.name
        for field in _FIELDS:
            setattr(row, field, getattr(spec, field))
        for field in _LISTS:
            setattr(row, field, list(getattr(spec, field)))
        for field in _MAPS:
            setattr(row, field, dict(getattr(spec, field)))
        row.provider_overrides = dict(spec.provider_overrides) if spec.provider_overrides else None
        row.initial_messages = list(spec.initial_messages) if spec.initial_messages is not None else None
        row.enabled_skills = [s.model_dump() for s in spec.enabled_skills]
        row.enabled_stores = [s.model_dump() for s in spec.enabled_stores]
        row.enabled_collections = [c.model_dump() for c in spec.enabled_collections]
        row.hooks = dict(spec.hooks) if spec.hooks is not None else None
        row.tool_approvals = dict(spec.tool_approvals) if spec.tool_approvals else None
        await self._check_mcp_bindings(spec, ctx, current)
        row.enabled_mcp_servers = [b.model_dump() for b in spec.enabled_mcp_servers]
        row.is_default = spec.is_default

    async def _check_mcp_bindings(
        self, spec: AgentSpec, ctx: ApplyContext, current: Optional[AgentSpec]
    ) -> None:
        """A binding is accepted only for a server that exists and that the
        acting user can read: the chat runtime applies the same check, so a
        binding nobody could use would only produce a warning per turn — and
        a user must not be able to point an agent at another owner's server
        (and credentials) by name. Only bindings added by this write are
        checked, so an unchanged agent never fails on a server that went away."""
        from app.core.auth import get_user_permissions
        from app.models.mcp_server import McpServer

        before = {b.server for b in (current.enabled_mcp_servers if current else [])}
        added = [b for b in spec.enabled_mcp_servers if b.server not in before]
        if not added:
            return
        actor = ctx.actor_user_id or ctx.owner_user_id
        permissions = await get_user_permissions(ctx.db, str(actor)) if actor else {}
        for binding in added:
            parts = binding.server.split("/", 1)
            if len(parts) != 2 or not all(parts):
                raise ApplierError(f"Invalid MCP server reference '{binding.server}' (use namespace/name)")
            if binding.server in ctx.pending_references.get("mcpServers", {}):
                continue  # declared by the same config (applied before agents)
            server = await McpServer.get_by_name(ctx.db, parts[0], parts[1])
            if server is None:
                raise ReferenceNotFound(f"MCP server '{binding.server}' not found")
            if actor and not server.can_user_access(str(actor), permissions, "read"):
                raise McpServerNotUsable(f"Not authorized to use MCP server '{binding.server}'")

    async def _unset_other_defaults(self, row: Agent, ctx: ApplyContext) -> None:
        from app.schemas.spec.base import diff_specs
        from app.services.resources.base import lock_singleton
        from app.services.resources.history import record_revision

        await lock_singleton(ctx, self.singleton_lock)
        stmt = select(Agent).where(Agent.is_default.is_(True)).with_for_update()
        if row.id is not None:
            stmt = stmt.where(Agent.id != row.id)
        for other in (await ctx.db.execute(stmt)).scalars().all():
            before = await self.current_spec(ctx, other)
            after = before.model_copy(update={"is_default": False})
            other.is_default = False
            state = self.history_spec(after)
            await record_revision(
                ctx, self, other, "update", state, diff_specs(self.history_spec(before), state)
            )

    async def check_references(self, spec: AgentSpec, ctx: ApplyContext) -> None:
        # A preview accepts a provider the same config declares.
        if spec.llm_provider_name and not ctx.declared("llmProviders", spec.llm_provider_name):
            await self._provider_id(ctx, spec.llm_provider_name)

    async def check_new_references(
        self, spec: AgentSpec, ctx: ApplyContext, current: Optional[AgentSpec]
    ) -> None:
        """Everything an agent can use must exist before it's added to the
        agent: create the agent, create its tools, then add them. Only what
        this write adds is checked, so an agent holding an older reference to
        something since removed can still be edited. Wildcards ("ns/*") and
        legacy agent ids aren't references to one thing and pass; so does
        anything the same config declares (it's applied all-or-nothing)."""
        if spec.llm_provider_name and (current is None or current.llm_provider_name != spec.llm_provider_name):
            await self.check_references(spec, ctx)
        missing: list[str] = []
        # The stored state in the spec's own spelling (e.g. a bare "lookup"
        # stored long ago reads as "default/lookup"), so an unchanged
        # reference never looks added.
        before = _normalized(current)
        for kind, label, model in _reference_kinds():
            added = _refs(spec, kind) - _refs(before, kind)
            wanted = []
            for ref in sorted(added):
                if "*" in ref or (kind == "agents" and (ref == spec.key or _is_uuid(ref))):
                    continue
                if "/" not in ref:
                    missing.append(f"{label} '{ref}' (name it as namespace/name)")
                elif ref in ctx.pending_references.get(kind, {}):
                    continue  # declared by the same config
                else:
                    wanted.append(ref)
            if wanted:
                found = await _existing(ctx, model, wanted)
                missing += [f"{label} '{ref}'" for ref in wanted if ref not in found]
        if missing:
            raise MissingAgentReferences(
                f"Agent '{spec.key}' refers to things that don't exist (create them first, "
                f"then add them to the agent): {', '.join(missing)}"
            )

    async def delete(self, row: Agent, ctx: ApplyContext) -> None:
        """Hard delete (package uninstall, or an upgrade that drops it). Chats
        outlive it: a conversation is the user's, so only their link to the
        agent is cleared (they keep its namespace/name)."""
        if not ctx.dry_run:
            from app.models.chat import Chat

            await ctx.db.execute(update(Chat).where(Chat.agent_id == row.id).values(agent_id=None))
        await super().delete(row, ctx)


async def provider_name(db, provider_id) -> Optional[str]:
    if provider_id is None:
        return None
    return (
        await db.execute(select(LLMProvider.name).where(LLMProvider.id == provider_id))
    ).scalar_one_or_none()


class MissingAgentReferences(ApplierError):
    status_code = 400


class McpServerNotUsable(ApplierError):
    status_code = 403


def _reference_kinds():
    from app.models.component import Component
    from app.models.connector import Connector
    from app.models.file import Collection
    from app.models.function import Function
    from app.models.pipeline import Pipeline
    from app.models.query import Query
    from app.models.skill import Skill
    from app.models.store import Store

    return (
        ("functions", "function", Function),
        ("skills", "skill", Skill),
        ("queries", "query", Query),
        ("stores", "store", Store),
        ("collections", "collection", Collection),
        ("components", "component", Component),
        ("connectors", "connector", Connector),
        ("pipelines", "pipeline", Pipeline),
        ("agents", "agent", Agent),
    )


def _refs(spec: Optional[AgentSpec], kind: str) -> set[str]:
    if spec is None:
        return set()
    if kind == "functions":
        refs = list(spec.enabled_functions)
        for entries in (spec.hooks or {}).values():
            refs += [e.get("function") for e in entries or [] if isinstance(e, dict) and e.get("function")]
        return set(refs)
    if kind == "skills":
        return {s.skill for s in spec.enabled_skills}
    if kind == "stores":
        return {s.store for s in spec.enabled_stores}
    if kind == "collections":
        return {c.collection for c in spec.enabled_collections}
    if kind == "connectors":
        return {c.get("connector") for c in spec.enabled_connectors if isinstance(c, dict) and c.get("connector")}
    field = {"queries": "enabled_queries", "components": "enabled_components",
             "pipelines": "enabled_pipelines", "agents": "enabled_agents"}[kind]
    return {r for r in getattr(spec, field) if isinstance(r, str)}


def _normalized(spec: Optional[AgentSpec]) -> Optional[AgentSpec]:
    if spec is None:
        return None
    try:
        return AgentSpec.model_validate(spec.model_dump())
    except Exception:  # stored data the spec no longer accepts: compare as is
        return spec


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


async def _existing(ctx: ApplyContext, model, refs: list[str]) -> set[str]:
    """Which of these "namespace/name" refs exist (in any state: a disabled
    tool is still the agent's to use once it's back on)."""
    from sqlalchemy import and_, or_

    pairs = [ref.split("/", 1) for ref in refs]
    rows = (
        await ctx.db.execute(
            select(model.namespace, model.name).where(
                or_(*(and_(model.namespace == ns, model.name == name) for ns, name in pairs))
            )
        )
    ).all()
    return {f"{ns}/{name}" for ns, name in rows}
