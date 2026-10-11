"""An agent's tools must exist before they're added to it: create the agent,
create its tools, then add them. Only what a write adds is checked, so an
agent holding an older reference to something since removed can still be
edited; wildcards and anything the same config declares pass."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.function import Function
from app.schemas.config import SinasConfig
from app.services.config_apply.service import ConfigApplyService
from tests.conftest import auth_headers

NS = "refs"


def _uid() -> str:
    return uuid.uuid4().hex[:8]


async def _apply(db, owner, dry_run=False, **spec):
    config = SinasConfig.model_validate({
        "apiVersion": "sinas.co/v1", "kind": "SinasConfig", "metadata": {"name": "cfg"}, "spec": spec,
    })
    return await ConfigApplyService(
        db, "cfg", owner_user_id=str(owner.id), managed_by="config", auto_commit=False
    ).apply_config(config, dry_run=dry_run)


async def _function(db, owner, name):
    db.add(Function(user_id=owner.id, namespace=NS, name=name, code="def handler(i, c): return {}",
                    input_schema={}, output_schema={}))
    await db.flush()


class TestRest:
    async def test_create_then_add_tools(self, client, db: AsyncSession, admin_user):
        name, tool, h = f"a{_uid()}", f"f{_uid()}", auth_headers(admin_user)
        r = await client.post(
            "/api/v1/agents", json={"namespace": NS, "name": name, "enabled_functions": [f"{NS}/{tool}"]}, headers=h
        )
        assert r.status_code == 400 and "don't exist" in r.text and f"{NS}/{tool}" in r.text
        assert (await client.post("/api/v1/agents", json={"namespace": NS, "name": name}, headers=h)).status_code == 201
        await _function(db, admin_user, tool)
        r = await client.put(f"/api/v1/agents/{NS}/{name}", json={"enabled_functions": [f"{NS}/{tool}"]}, headers=h)
        assert r.status_code == 200, r.text

    async def test_every_kind_is_checked_and_named(self, client, admin_user):
        r = await client.post("/api/v1/agents", json={
            "namespace": NS, "name": f"a{_uid()}",
            "enabled_skills": [{"skill": f"{NS}/nope"}], "enabled_queries": [f"{NS}/nope"],
            "enabled_stores": [{"store": f"{NS}/nope"}], "enabled_collections": [{"collection": f"{NS}/nope"}],
            "enabled_components": [f"{NS}/nope"], "enabled_connectors": [{"connector": f"{NS}/nope"}],
            "enabled_pipelines": [f"{NS}/nope"], "enabled_agents": [f"{NS}/nope"],
            "hooks": {"on_user_message": [{"function": f"{NS}/nope"}]},
        }, headers=auth_headers(admin_user))
        assert r.status_code == 400
        for label in ("skill", "query", "store", "collection", "component", "connector", "pipeline", "agent", "function"):
            assert f"{label} '{NS}/nope'" in r.text, label

    async def test_wildcards_self_and_older_references_pass(self, client, db: AsyncSession, admin_user):
        name, h = f"a{_uid()}", auth_headers(admin_user)
        r = await client.post("/api/v1/agents", json={
            "namespace": NS, "name": name, "enabled_functions": [f"{NS}/*"], "enabled_agents": [f"{NS}/{name}", "*/*"],
        }, headers=h)
        assert r.status_code == 201, r.text
        # An agent whose tool was removed later can still be edited.
        db.add(Agent(namespace=NS, name=f"old{_uid()}", user_id=admin_user.id, enabled_functions=[f"{NS}/gone"]))
        await db.flush()
        old = (await db.execute(
            Agent.__table__.select().where(Agent.namespace == NS, Agent.name.like("old%"))
        )).first()
        r = await client.put(f"/api/v1/agents/{NS}/{old.name}", json={"description": "still editable"}, headers=h)
        assert r.status_code == 200, r.text


class TestConfigApply:
    async def test_tools_declared_in_the_same_config_count(self, db: AsyncSession, admin_user):
        a, b, tool = f"a{_uid()}", f"b{_uid()}", f"f{_uid()}"
        spec = dict(
            functions=[{"namespace": NS, "name": tool, "code": "def handler(i, c): return {}"}],
            stores=[{"namespace": NS, "name": "notes"}],
            agents=[
                # a uses b, declared after it in the same config
                {"namespace": NS, "name": a, "enabledAgents": [f"{NS}/{b}"], "enabledFunctions": [f"{NS}/{tool}"]},
                {"namespace": NS, "name": b, "enabledStores": [f"{NS}/notes"]},
            ],
        )
        for dry_run in (True, False):
            result = await _apply(db, admin_user, dry_run=dry_run, **spec)
            assert result.success, result.errors

    async def test_a_missing_tool_fails_the_apply(self, db: AsyncSession, admin_user):
        result = await _apply(db, admin_user, agents=[
            {"namespace": NS, "name": f"a{_uid()}", "enabledFunctions": [f"{NS}/missing"]},
        ])
        assert not result.success and "don't exist" in result.errors[0]


class TestReviewFixes:
    async def test_config_can_edit_an_agent_with_an_older_dangling_reference(self, db: AsyncSession, admin_user):
        name = f"a{_uid()}"
        await _function(db, admin_user, "gone")
        spec = {"namespace": NS, "name": name, "enabledFunctions": [f"{NS}/gone"]}
        assert (await _apply(db, admin_user, agents=[spec])).success
        gone = (await db.execute(
            Function.__table__.select().where(Function.namespace == NS, Function.name == "gone")
        )).first()
        await db.execute(Function.__table__.delete().where(Function.id == gone.id))
        result = await _apply(db, admin_user, agents=[{**spec, "description": "still editable"}])
        assert result.success, result.errors

    async def test_a_bare_stored_name_isnt_an_addition(self, client, db: AsyncSession, admin_user):
        name = f"a{_uid()}"
        db.add(Agent(namespace=NS, name=name, user_id=admin_user.id, enabled_functions=["lookup"]))
        await db.flush()
        r = await client.put(f"/api/v1/agents/{NS}/{name}", json={"description": "d"}, headers=auth_headers(admin_user))
        assert r.status_code == 200, r.text


async def test_validate_reports_a_missing_tool(client, admin_user):
    yaml_text = "\n".join([
        "apiVersion: sinas.co/v1", "kind: SinasConfig", "metadata: {name: cfg}", "spec:",
        "  agents:", f"    - {{namespace: {NS}, name: a{_uid()}, enabledFunctions: [{NS}/missing]}}",
    ])
    r = await client.post("/api/v1/config/validate", json={"config": yaml_text}, headers=auth_headers(admin_user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is False and "don't exist" in body["errors"][0]["message"]
