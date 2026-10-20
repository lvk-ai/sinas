"""Wildcards ("namespace/*", "*/*") in every agent enabled_* list (#174).

Components, connectors and stores used to look their entries up exactly, so
a wildcard there was silently dropped. They now resolve as functions do,
through the shared resolver. Contracts under test, per kind:

- a wildcard expands to the matching active resources, and only to those
  the chat's user may use themselves (the permission the runtime checks for
  that kind); an exact entry is the author's grant and behaves as before;
- execution re-checks a call against the agent's current list, so a call
  can't reach what discovery would not have offered;
- entries with an access mode (stores, collections) carry it to every
  resource a wildcard matches; the most specific entry decides one
  resource's mode, and read-write needs the user's write permission;
- a "*" that no resolution could serve is refused at validation (REST and
  config apply) instead of being ignored at runtime.
"""
import uuid

import pytest

from app.models.component import Component
from app.models.connector import Connector
from app.models.file import Collection
from app.models.function import Function
from app.models.skill import Skill
from app.models.store import Store
from app.models.user import Role, RolePermission, User, UserRole
from app.services.collection_tools import CollectionToolConverter
from app.services.component_tools import ComponentToolConverter
from app.services.connector_tools import ConnectorToolConverter
from app.services.function_tools import FunctionToolConverter
from app.services.resource_resolver import (
    ALL,
    EXACT,
    NAMESPACE,
    matches_ref_pattern,
    ref_access,
    ref_specificity,
    wildcard_ref_problem,
)
from app.services.skill_tools import SkillToolConverter
from app.services.state_tools import StateTools
from tests.conftest import auth_headers


def _uid() -> str:
    return uuid.uuid4().hex[:8]


async def _user(db, *permissions: str) -> User:
    role = Role(name=f"wc-{_uid()}", description="wildcard test role")
    db.add(role)
    await db.flush()
    for key in permissions:
        db.add(RolePermission(role_id=role.id, permission_key=key, permission_value=True))
    user = User(email=f"wc-{_uid()}@example.com")
    db.add(user)
    await db.flush()
    db.add(UserRole(role_id=role.id, user_id=user.id, active=True))
    await db.flush()
    return user


def _names(tools) -> set[str]:
    return {t["function"]["name"] for t in tools}


# ------------------------------------------------------------ patterns


def test_pattern_shapes():
    assert ref_specificity("ns/name") == EXACT
    assert ref_specificity("ns/*") == NAMESPACE
    assert ref_specificity("*/*") == ALL
    for unresolvable in ("*/name", "ns/a*", "n*/*", "*", "ns/", "/x"):
        assert ref_specificity(unresolvable) is None, unresolvable
    assert wildcard_ref_problem("ns/*") is None
    assert wildcard_ref_problem("ns/name") is None
    assert "not a supported pattern" in wildcard_ref_problem("*/name")
    assert "not a supported pattern" in wildcard_ref_problem("ns/a*")


def test_matching_and_the_most_specific_entry_deciding_access():
    entries = [
        {"store": "*/*", "access": "readonly"},
        {"store": "crm/*", "access": "readwrite"},
        {"store": "crm/archive", "access": "readonly"},
    ]
    assert ref_access("crm/notes", entries) == "readwrite"  # ns/* beats */*
    assert ref_access("crm/archive", entries) == "readonly"  # exact beats ns/*
    assert ref_access("hr/people", entries) == "readonly"  # only */* matches
    assert ref_access("hr/people", entries[1:]) is None
    # Equally specific entries combine: any readwrite wins (as duplicate
    # exact store entries always did).
    assert ref_access("a/b", [{"store": "a/b", "access": "readonly"}, {"store": "a/b", "access": "readwrite"}]) == "readwrite"
    assert matches_ref_pattern("crm/x", [{"connector": "crm/*"}])
    assert matches_ref_pattern("crm/x", [{"server": "*/*"}])
    assert not matches_ref_pattern("crm/x", [{"connector": "*/x"}])  # not a pattern


# ------------------------------------------------------------ components


def _component(owner, ns, name, **extra) -> Component:
    return Component(user_id=owner.id, namespace=ns, name=name, source_code="<p>x</p>", **extra)


@pytest.mark.asyncio
async def test_component_wildcards_offer_only_what_the_user_may_read(db, admin_user):
    viewer = await _user(db, "sinas.components/*/*.read:own")
    ns, other = f"wc{_uid()}", f"wc{_uid()}"
    db.add_all([
        _component(admin_user, ns, "private"),  # someone else's, private
        _component(admin_user, ns, "shared", visibility="shared"),
        _component(viewer, ns, "mine"),
        _component(viewer, ns, "off", is_active=False),
        _component(viewer, other, "elsewhere"),
    ])
    await db.flush()
    conv = ComponentToolConverter()

    def tool(namespace, name):
        return f"show_component_{namespace}__{name}"

    tools = await conv.get_available_components(db, [f"{ns}/*"], user_id=str(viewer.id))
    assert _names(tools) == {tool(ns, "shared"), tool(ns, "mine")}
    everything = await conv.get_available_components(db, ["*/*"], user_id=str(viewer.id))
    assert {tool(ns, "shared"), tool(ns, "mine"), tool(other, "elsewhere")} <= _names(everything)
    assert tool(ns, "private") not in _names(everything)
    # An exact entry is the author's grant, as before; the wildcard doesn't
    # take it away.
    exact = await conv.get_available_components(db, [f"{ns}/*", f"{ns}/private"], user_id=str(viewer.id))
    assert _names(exact) == {tool(ns, "shared"), tool(ns, "mine"), tool(ns, "private")}
    # No user, no wildcard expansion.
    assert await conv.get_available_components(db, [f"{ns}/*"]) == []
    # Someone with read:all gets everything active.
    assert len(await conv.get_available_components(db, [f"{ns}/*"], user_id=str(admin_user.id))) == 3


@pytest.mark.asyncio
async def test_component_calls_are_checked_against_the_agent_list(db, admin_user):
    viewer = await _user(db, "sinas.components/*/*.read:own")
    ns = f"wc{_uid()}"
    db.add_all([_component(admin_user, ns, "private"), _component(admin_user, ns, "shared", visibility="shared")])
    await db.flush()
    conv = ComponentToolConverter()

    async def show(name, enabled):
        return await conv.handle_component_tool_call(
            db, f"show_component_{ns}__{name}", {}, user_id=str(viewer.id), enabled_components=enabled
        )

    assert (await show("shared", [f"{ns}/*"]))["name"] == "shared"
    assert await show("private", [f"{ns}/*"]) is None  # not readable: the wildcard doesn't reach it
    assert (await show("private", [f"{ns}/private"]))["name"] == "private"  # named: as before
    assert await show("shared", []) is None  # not enabled at all
    assert await show("shared", ["other/*"]) is None


# ------------------------------------------------------------ connectors


def _connector(owner, ns, name) -> Connector:
    ops = [
        {"name": op, "method": "GET", "path": f"/{op}", "parameters": {"type": "object", "properties": {"x": {"type": "string"}}}}
        for op in ("list", "get", "post")
    ]
    return Connector(user_id=owner.id, namespace=ns, name=name, base_url="https://api.example.test", operations=ops)


@pytest.mark.asyncio
async def test_connector_wildcard_entries_apply_to_each_readable_connector(db, admin_user):
    user = await _user(db, "sinas.connectors/*/*.read:own")  # their own connectors only
    ns = f"wc{_uid()}"
    db.add_all([_connector(user, ns, "one"), _connector(user, ns, "two"), _connector(admin_user, ns, "theirs")])
    await db.flush()
    entries = [
        {"connector": f"{ns}/*", "operations": ["get"]},
        # An exact entry decides for its connector: other operations, locked parameters.
        {"connector": f"{ns}/two", "operations": ["post"], "parameters": {"post": {"x": "fixed"}}},
    ]
    conv = ConnectorToolConverter()
    tools = await conv.get_available_connectors(db, entries, user_id=str(user.id))
    assert _names(tools) == {f"connector__{ns}__one__get", f"connector__{ns}__two__post"}
    post = next(t for t in tools if t["function"]["name"].endswith("__post"))
    assert post["function"]["_metadata"]["locked_params"] == {"x": "fixed"}
    assert "x" not in post["function"]["parameters"]["properties"]

    # Someone who may read every connector gets "theirs" through the wildcard too.
    admin_tools = await conv.get_available_connectors(db, entries, user_id=str(admin_user.id))
    assert f"connector__{ns}__theirs__get" in _names(admin_tools)
    # Exact entries are the author's grant, as before.
    exact = await conv.get_available_connectors(db, [{"connector": f"{ns}/theirs"}], user_id=str(user.id))
    assert _names(exact) == {f"connector__{ns}__theirs__{op}" for op in ("list", "get", "post")}


@pytest.mark.asyncio
async def test_connector_calls_are_checked_against_the_agent_list(db, admin_user, monkeypatch):
    from app.services import connector_tools

    user = await _user(db, "sinas.connectors/*/*.read:own")
    ns = f"wc{_uid()}"
    db.add_all([_connector(user, ns, "one"), _connector(admin_user, ns, "theirs")])
    await db.flush()

    async def fake_execute(**kwargs):
        return {"status_code": 200, "ran": kwargs["operation_name"]}

    monkeypatch.setattr(connector_tools.connector_service, "execute_operation", fake_execute)
    conv = ConnectorToolConverter()
    entries = [{"connector": f"{ns}/*", "operations": ["get"]}]

    async def call(name, op, enabled):
        return await conv.execute_connector_tool(
            db, f"connector__{ns}__{name}__{op}", {}, user_id=str(user.id), enabled_connectors=enabled
        )

    assert (await call("one", "get", entries))["ran"] == "get"
    assert "not enabled" in (await call("one", "post", entries))["error"]  # outside the entry's operations
    assert "not enabled" in (await call("theirs", "get", entries))["error"]  # not readable: wildcard stops
    assert (await call("theirs", "get", [{"connector": f"{ns}/theirs"}]))["ran"] == "get"  # named: as before
    assert "not enabled" in (await call("one", "get", []))["error"]


# ------------------------------------------------------------ stores


@pytest.mark.asyncio
async def test_store_wildcards_carry_their_mode_within_the_users_permissions(db, admin_user):
    ns, other = f"crm{_uid()}", f"hr{_uid()}"
    user = await _user(
        db,
        f"sinas.stores/{ns}/*.read_state:own",
        f"sinas.stores/{ns}/notes.write_state:own",
    )
    db.add_all([
        Store(namespace=ns, name="notes", user_id=admin_user.id),
        Store(namespace=ns, name="prefs", user_id=admin_user.id),
        Store(namespace=ns, name="archive", user_id=admin_user.id),
        Store(namespace=other, name="secrets", user_id=admin_user.id),
    ])
    await db.flush()
    entries = [
        {"store": "*/*", "access": "readwrite"},
        {"store": f"{ns}/archive", "access": "readonly"},
    ]
    resolved = {ref: access for ref, access, _ in await StateTools.resolve_stores(db, str(user.id), entries)}
    assert resolved == {
        f"{ns}/notes": "readwrite",
        f"{ns}/prefs": "readonly",  # no write_state: offered read only
        f"{ns}/archive": "readonly",  # the exact entry decides
    }  # hr/secrets: no read_state, never offered

    tools = {t["function"]["name"]: t["function"] for t in await StateTools.get_tool_definitions(db, str(user.id), entries)}
    assert tools["save_state"]["parameters"]["properties"]["store"]["enum"] == [f"{ns}/notes"]
    assert set(tools["retrieve_state"]["parameters"]["properties"]["store"]["enum"]) == set(resolved)


@pytest.mark.asyncio
async def test_store_execution_resolves_wildcards_as_discovery_does(db, admin_user):
    from app.models.agent import Agent

    ns = f"crm{_uid()}"
    user = await _user(db, f"sinas.stores/{ns}/*.read_state:own", f"sinas.stores/{ns}/notes.write_state:own")
    db.add_all([Store(namespace=ns, name="notes", user_id=admin_user.id), Store(namespace=ns, name="prefs", user_id=admin_user.id)])
    agent = Agent(user_id=admin_user.id, namespace=ns, name="a", enabled_stores=[{"store": f"{ns}/*", "access": "readwrite"}])
    db.add(agent)
    await db.flush()

    async def run(tool, **args):
        return await StateTools.execute_tool(db, tool, args, str(user.id), agent_id=str(agent.id))

    saved = await run("save_state", store=f"{ns}/notes", key="k", value={"v": 1})
    assert saved.get("success"), saved
    refused = await run("save_state", store=f"{ns}/prefs", key="k", value=1)
    assert "not authorized to write" in refused["error"]
    found = await run("retrieve_state", store=f"{ns}/notes", key="k")
    assert found["count"] == 1
    listed = await run("list_state_keys")
    assert {s["store"] for s in listed["stores"]} == {f"{ns}/notes"}


@pytest.mark.asyncio
async def test_exact_store_entries_behave_as_before(db, admin_user):
    """An exact entry is offered whatever the user's permissions (execution
    checks them, as it always did)."""
    ns = f"crm{_uid()}"
    nobody = await _user(db)
    db.add(Store(namespace=ns, name="notes", user_id=admin_user.id))
    await db.flush()
    entries = [{"store": f"{ns}/notes", "access": "readwrite"}]
    tools = {t["function"]["name"]: t["function"] for t in await StateTools.get_tool_definitions(db, str(nobody.id), entries)}
    assert tools["save_state"]["parameters"]["properties"]["store"]["enum"] == [f"{ns}/notes"]
    # ... and a wildcard alone gives this user nothing.
    assert await StateTools.get_tool_definitions(db, str(nobody.id), [{"store": f"{ns}/*", "access": "readwrite"}]) == []


# ------------------------------------------------------------ collections


@pytest.mark.asyncio
async def test_collection_wildcards_follow_the_same_rules(db, admin_user):
    ns = f"docs{_uid()}"
    user = await _user(db, f"sinas.collections/{ns}/*.download:own", f"sinas.collections/{ns}/drafts.upload:own")
    db.add_all([
        Collection(namespace=ns, name="drafts", user_id=admin_user.id),
        Collection(namespace=ns, name="final", user_id=admin_user.id),
        Collection(namespace=ns, name="ro", user_id=admin_user.id),
    ])
    await db.flush()
    entries = [{"collection": f"{ns}/*", "access": "readwrite"}, {"collection": f"{ns}/ro", "access": "readonly"}]
    names = _names(await CollectionToolConverter().get_available_collections(db, str(user.id), entries))
    for coll in ("drafts", "final", "ro"):
        assert f"search_collection_{ns}_{coll}" in names
    assert f"write_file_{ns}_drafts" in names
    assert f"write_file_{ns}_final" not in names  # no upload permission: read only
    assert f"write_file_{ns}_ro" not in names  # the exact entry decides

    stranger = await _user(db)
    assert await CollectionToolConverter().get_available_collections(db, str(stranger.id), entries[:1]) == []


# ------------------------------------------------------------ skills, functions


@pytest.mark.asyncio
async def test_skill_wildcards_reach_only_readable_skills(db, admin_user):
    user = await _user(db, "sinas.skills/*/*.read:own")  # their own skills
    ns = f"sk{_uid()}"
    db.add_all([
        Skill(user_id=user.id, namespace=ns, name="mine", description="d", content="mine-content"),
        Skill(user_id=admin_user.id, namespace=ns, name="theirs", description="d", content="theirs-content"),
    ])
    await db.flush()
    conv = SkillToolConverter()
    tools = await conv.get_available_skills(db, [{"skill": f"{ns}/*"}], user_id=str(user.id))
    assert _names(tools) == {f"get_skill_{ns}_mine"}
    preloaded = await conv.get_preloaded_skills_content(db, [{"skill": f"{ns}/*", "preload": True}], user_id=str(user.id))
    assert "mine-content" in preloaded and "theirs-content" not in preloaded
    # Named exactly: the author's grant, as before.
    exact = await conv.get_available_skills(db, [{"skill": f"{ns}/theirs"}], user_id=str(user.id))
    assert _names(exact) == {f"get_skill_{ns}_theirs"}
    # Calls are checked against the agent's list the same way.
    call = conv.handle_skill_tool_call
    assert await call(db, f"get_skill_{ns}_theirs", {}, enabled_skills=[{"skill": f"{ns}/*"}], user_id=str(user.id)) is None
    assert await call(db, f"get_skill_{ns}_theirs", {}, enabled_skills=[{"skill": f"{ns}/theirs"}], user_id=str(user.id)) == "theirs-content"
    assert await call(db, f"get_skill_{ns}_mine", {}, enabled_skills=[{"skill": f"{ns}/*"}], user_id=str(user.id)) == "mine-content"


@pytest.mark.asyncio
async def test_function_wildcards_offer_only_executable_functions(db, admin_user):
    user = await _user(db, "sinas.functions/*/*.execute:own")  # their own functions
    ns = f"fn{_uid()}"

    def fn(owner, name):
        return Function(
            user_id=owner.id, namespace=ns, name=name, code="def handler(input, context):\n    return {}\n",
            input_schema={"type": "object"}, output_schema={"type": "object"},
        )

    db.add_all([fn(user, "mine"), fn(admin_user, "theirs")])
    await db.flush()
    conv = FunctionToolConverter()
    tools = await conv.get_available_functions(db, str(user.id), [f"{ns}/*"])
    assert _names(tools) == {f"{ns}__mine"}
    exact = await conv.get_available_functions(db, str(user.id), [f"{ns}/theirs"])
    assert _names(exact) == {f"{ns}__theirs"}


# ------------------------------------------------------------ validation


@pytest.mark.asyncio
async def test_rest_refuses_patterns_nothing_could_resolve(client, admin_user):
    headers = auth_headers(admin_user)
    ns = f"wc{_uid()}"

    async def create(name, **fields):
        return await client.post(
            "/api/v1/agents", json={"namespace": ns, "name": name, "system_prompt": "x", **fields}, headers=headers
        )

    refused = {
        "components": {"enabled_components": ["*/dashboard"]},
        "stores": {"enabled_stores": [{"store": "crm/no*", "access": "readonly"}]},
        "connectors": {"enabled_connectors": [{"connector": "*/slack"}]},
        "functions": {"enabled_functions": ["sales/send*"]},
        "hooks": {"hooks": {"on_user_message": [{"function": "guards/*"}]}},
        "parameters": {"function_parameters": {"sales/*": {"region": "eu"}}},
        "mcp": {"enabled_mcp_servers": [{"server": "tools/a*"}]},
    }
    for label, fields in refused.items():
        response = await create(f"bad-{label}", **fields)
        assert response.status_code == 400, (label, response.text)
        assert "*" in response.json()["detail"], (label, response.text)

    accepted = await create(
        "good",
        enabled_components=[f"{ns}/*"],
        enabled_connectors=[{"connector": "*/*", "operations": ["get"]}],
        enabled_stores=[{"store": f"{ns}/*", "access": "readwrite"}],
        enabled_mcp_servers=[{"server": "tools/*"}],
        enabled_collections=[{"collection": "*/*", "access": "readonly"}],
    )
    assert accepted.status_code == 201, accepted.text
    body = accepted.json()
    assert body["enabled_components"] == [f"{ns}/*"]
    assert body["enabled_stores"] == [{"store": f"{ns}/*", "access": "readwrite"}]

    response = await client.put(f"/api/v1/agents/{ns}/good", json={"enabled_components": ["*/x"]}, headers=headers)
    assert response.status_code == 400, response.text
    # A parameters-only update is checked too.
    for field in ("function_parameters", "query_parameters"):
        response = await client.put(
            f"/api/v1/agents/{ns}/good", json={field: {"sales/*": {"region": "eu"}}}, headers=headers
        )
        assert response.status_code == 400, (field, response.text)
        assert "parameters are set per" in response.json()["detail"]


@pytest.mark.asyncio
async def test_config_apply_refuses_patterns_nothing_could_resolve(db, admin_user):
    from app.services.config_apply.service import ConfigApplyService
    from tests.unit.config_parity.test_mcp_servers import _config

    def agent(**fields):
        return {"namespace": f"wc{_uid()}", "name": "a", "systemPrompt": "x", **fields}

    def service():
        return ConfigApplyService(db, "cfg", owner_user_id=str(admin_user.id), managed_by="config", auto_commit=False)

    result = await service().apply_config(_config({"agents": [agent(enabledComponents=["*/dashboard"])]}), dry_run=True)
    assert not result.success
    assert "'*/dashboard' is not a supported pattern" in " ".join(result.errors)

    result = await service().apply_config(_config({"agents": [agent(
        enabledComponents=["ui/*"],
        enabledConnectors=[{"connector": "apis/*", "operations": ["get"]}],
        enabledStores=[{"store": "*/*", "access": "readonly"}],
    )]}), dry_run=True)
    assert result.success, result.errors
