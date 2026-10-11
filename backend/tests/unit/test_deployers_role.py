"""The built-in Deployers role: what a CI/CD deploy key needs, nothing more,
linked to API keys (no members by default), and protected like the other
default roles."""

from sqlalchemy import select

from app.core.auth import initialize_default_roles
from app.models.user import Role, RolePermission, UserRole
from tests.conftest import auth_headers

CONFIG = "\n".join([
    "apiVersion: sinas.co/v1", "kind: SinasConfig", "metadata: {name: ci}", "spec:",
    "  templates:", "    - {namespace: ci, name: hello, htmlContent: '<p/>'}",
]) + "\n"


async def _deployers(db):
    await initialize_default_roles(db)
    return (await db.execute(select(Role).where(Role.name == "Deployers"))).scalar_one()


async def test_seeded_with_exactly_the_deploy_permissions_and_no_members(db):
    role = await _deployers(db)
    perms = dict((await db.execute(
        select(RolePermission.permission_key, RolePermission.permission_value).where(RolePermission.role_id == role.id)
    )).all())
    assert perms == {
        "sinas.config.validate:all": True, "sinas.config.apply:all": True, "sinas.packages.install:all": True,
    }
    assert "CI/CD" in role.description
    assert (await db.execute(select(UserRole).where(UserRole.role_id == role.id))).first() is None


async def test_a_key_linked_to_it_deploys_and_does_nothing_else(client, db, admin_user):
    role = await _deployers(db)
    r = await client.post(
        "/api/v1/api-keys", json={"name": "ci-deploy", "permissions": {}, "role_ids": [str(role.id)]},
        headers=auth_headers(admin_user),
    )
    assert r.status_code == 201, r.text
    h = {"X-API-Key": r.json()["key"]}
    r = await client.post("/api/v1/config/apply", json={"config": CONFIG}, headers=h)
    assert r.status_code == 200 and r.json()["success"], r.text
    # Nothing beyond deploying: not even listing users or creating an agent.
    assert (await client.get("/api/v1/users", headers=h)).status_code == 403
    r = await client.post("/api/v1/agents", json={"namespace": "ci", "name": "x"}, headers=h)
    assert r.status_code == 403


async def test_it_cant_be_renamed_or_deleted(client, db, admin_user):
    await _deployers(db)
    h = auth_headers(admin_user)
    assert (await client.patch("/api/v1/roles/Deployers", json={"name": "Ops"}, headers=h)).status_code == 400
    assert (await client.delete("/api/v1/roles/Deployers", headers=h)).status_code == 400
