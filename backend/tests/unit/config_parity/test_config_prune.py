"""Opt-in prune on /config/apply: removes what the same config file applied
earlier but no longer declares — never another file's resources, never
something edited by hand since, never credentials or dependencies. A dry
run lists what would go."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config_revision import ConfigRevision
from app.models.secret import Secret
from app.models.template import Template
from tests.conftest import auth_headers

NS = "prune"


def _doc(name: str, templates: list[str], secrets: list[str] = ()) -> str:
    lines = ["apiVersion: sinas.co/v1", "kind: SinasConfig", f"metadata: {{name: {name}}}", "spec:"]
    lines.append("  templates:" + ("" if templates else " []"))
    lines += [f"    - {{namespace: {NS}, name: {t}, htmlContent: '<p/>'}}" for t in templates]
    if secrets:
        lines.append("  secrets:")
        lines += [f"    - {{name: {s}, value: v}}" for s in secrets]
    return "\n".join(lines) + "\n"


async def _apply(client, user, doc, **flags):
    r = await client.post("/api/v1/config/apply", json={"config": doc, **flags}, headers=auth_headers(user))
    assert r.status_code == 200, r.text
    return r.json()


async def _names(db: AsyncSession) -> set[str]:
    return set((await db.execute(select(Template.name).where(Template.namespace == NS))).scalars())


async def test_prune_removes_only_this_files_dropped_resources(client, db: AsyncSession, admin_user):
    a, b, other, edited = (f"{p}{uuid.uuid4().hex[:6]}" for p in ("a", "b", "o", "e"))
    secret = f"S_{uuid.uuid4().hex[:6].upper()}"
    assert (await _apply(client, admin_user, _doc("site", [a, b, edited], [secret])))["success"]
    assert (await _apply(client, admin_user, _doc("other", [other])))["success"]
    # A hand edit detaches `edited` from the config.
    row = (await db.execute(select(Template).where(Template.namespace == NS, Template.name == edited))).scalar_one()
    r = await client.patch(f"/api/v1/templates/{row.id}", json={"description": "ours"}, headers=auth_headers(admin_user))
    assert r.status_code == 200, r.text

    # Without prune, dropping b from the file leaves it alone.
    result = await _apply(client, admin_user, _doc("site", [a]))
    assert result["success"] and not result["summary"]["deleted"]
    assert {a, b, other, edited} <= await _names(db)

    # A dry run with prune lists it, and removes nothing.
    plan = await _apply(client, admin_user, _doc("site", [a]), prune=True, dryRun=True)
    assert plan["summary"]["deleted"] == {"templates": 1}
    assert b in await _names(db)

    result = await _apply(client, admin_user, _doc("site", [a]), prune=True)
    assert result["success"] and result["summary"]["deleted"] == {"templates": 1}
    names = await _names(db)
    assert b not in names and {a, other, edited} <= names
    # Credentials stay, and the removal is recorded (restorable).
    assert (await db.execute(select(Secret).where(Secret.name == secret))).scalar_one_or_none() is not None
    actions = (await db.execute(
        select(ConfigRevision.action).where(
            ConfigRevision.resource_kind == "templates", ConfigRevision.resource_key == f"{NS}/{b}"
        ).order_by(ConfigRevision.id)
    )).scalars().all()
    assert list(actions) == ["create", "delete"]


async def test_something_still_used_isnt_removed(client, db: AsyncSession, admin_user):
    fn, sched = f"f{uuid.uuid4().hex[:6]}", f"s-{uuid.uuid4().hex[:6]}"
    function = f"    - {{namespace: {NS}, name: {fn}, code: 'def handler(i, c): return {{}}'}}"
    schedule = f"    - {{name: {sched}, functionName: {NS}/{fn}, cronExpression: '0 3 * * *'}}"
    head = ["apiVersion: sinas.co/v1", "kind: SinasConfig", "metadata: {name: jobs}", "spec:"]
    both = "\n".join(head + ["  functions:", function, "  schedules:", schedule]) + "\n"
    schedule_only = "\n".join(head + ["  functions: []", "  schedules:", schedule]) + "\n"
    assert (await _apply(client, admin_user, both))["success"]
    for dry_run in (True, False):
        result = await _apply(client, admin_user, schedule_only, prune=True, dryRun=dry_run)
        assert not result["success"], dry_run
        assert any("still used by schedule" in e and fn in e for e in result["errors"]), result["errors"]


async def test_a_collection_with_files_is_kept(client, db: AsyncSession, admin_user):
    from app.models.file import Collection, File

    coll = f"c{uuid.uuid4().hex[:6]}"
    doc = lambda colls: "\n".join(  # noqa: E731
        ["apiVersion: sinas.co/v1", "kind: SinasConfig", "metadata: {name: files}", "spec:",
         "  collections:" + ("" if colls else " []")]
        + [f"    - {{namespace: {NS}, name: {c}}}" for c in colls]
    ) + "\n"
    assert (await _apply(client, admin_user, doc([coll])))["success"]
    row = await Collection.get_by_name(db, NS, coll)
    db.add(File(collection_id=row.id, name="a.txt", user_id=admin_user.id, content_type="text/plain"))
    await db.flush()
    result = await _apply(client, admin_user, doc([]), prune=True)
    assert result["success"] and not result["summary"]["deleted"]
    assert any("holds 1 file" in w for w in result["warnings"])
    assert await Collection.get_by_name(db, NS, coll) is not None
