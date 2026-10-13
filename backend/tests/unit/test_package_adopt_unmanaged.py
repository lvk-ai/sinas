"""adoptUnmanaged: a package install may take over what was edited by hand.

Without it, a package never adopts a row with managed_by NULL (design §4.4):
an operator who fixed a package's agent by hand had no way to hand it back,
and the package never updated it again. With the flag the install writes the
row and stamps it as the package's, reported as an update with a revision
and a warning. A row another package or config manages stays skipped.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.config_revision import ConfigRevision
from app.models.user import Role, User
from app.services.package_service import PackageService
from app.services.resources import ApplyContext
from app.services.resources.roles import RoleApplier
from tests.conftest import auth_headers

PACKAGE_YAML = """
apiVersion: sinas.co/v1
kind: SinasPackage
package:
  name: {pkg}
  version: "1.0.0"
spec:
  agents:
    - namespace: {ns}
      name: checker
      description: From the package
      systemPrompt: {prompt}
"""


def _names() -> tuple[str, str]:
    return f"pkg-{uuid.uuid4().hex[:8]}", f"ns{uuid.uuid4().hex[:6]}"


async def _agent(db: AsyncSession, ns: str) -> Agent:
    return (
        await db.execute(
            select(Agent)
            .where(Agent.namespace == ns, Agent.name == "checker")
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _revisions(db: AsyncSession, ns: str) -> list[ConfigRevision]:
    return list(
        (
            await db.execute(
                select(ConfigRevision)
                .where(ConfigRevision.resource_kind == "agents",
                       ConfigRevision.resource_key == f"{ns}/checker")
                .order_by(ConfigRevision.id)
            )
        ).scalars()
    )


async def _hand_edited(db: AsyncSession, owner: User, pkg: str, ns: str) -> str:
    """Install the package, then edit its agent by hand (which detaches it)."""
    source = PACKAGE_YAML.format(pkg=pkg, ns=ns, prompt="The package's prompt.")
    await PackageService(db).install(source, str(owner.id))
    agent = await _agent(db, ns)
    assert agent.managed_by == f"pkg:{pkg}"
    agent.system_prompt = "A quick fix by hand."
    agent.managed_by = agent.config_name = agent.config_checksum = None
    await db.flush()
    return PACKAGE_YAML.format(pkg=pkg, ns=ns, prompt="The package's improved prompt.")


async def test_a_hand_edited_agent_stays_skipped_without_the_flag(db: AsyncSession, admin_user: User):
    pkg, ns = _names()
    source = await _hand_edited(db, admin_user, pkg, ns)

    _package, result = await PackageService(db).install(source, str(admin_user.id))

    assert result.summary.skipped == {"agents": 1}, result.summary
    [change] = result.changes
    assert change.action == "skipped" and change.reason == "edited_by_hand"
    assert any("checker" in w and "adoptUnmanaged" in w for w in result.warnings), result.warnings
    agent = await _agent(db, ns)
    assert agent.system_prompt == "A quick fix by hand." and agent.managed_by is None


async def test_with_the_flag_it_is_taken_over_and_uninstall_deletes_it(db: AsyncSession, admin_user: User):
    pkg, ns = _names()
    source = await _hand_edited(db, admin_user, pkg, ns)
    before = len(await _revisions(db, ns))

    svc = PackageService(db)
    _package, result = await svc.install(source, str(admin_user.id), adopt_unmanaged=True)

    assert result.success, result.errors
    assert result.summary.updated == {"agents": 1} and not result.summary.skipped, result.summary
    [change] = result.changes
    assert change.action == "update" and change.reason == "adopted"
    assert "system_prompt" in (change.changes or {})
    assert any("checker" in w and f"pkg:{pkg}" in w and "uninstalling" in w for w in result.warnings)
    agent = await _agent(db, ns)
    assert agent.system_prompt == "The package's improved prompt."
    assert agent.managed_by == f"pkg:{pkg}" and agent.config_name == pkg

    revisions = await _revisions(db, ns)
    assert len(revisions) == before + 1
    assert revisions[-1].action == "update" and revisions[-1].managed_by == f"pkg:{pkg}"

    # The next install manages it as its own again, flag or not.
    _package, again = await svc.install(source, str(admin_user.id))
    assert again.summary.unchanged == {"agents": 1}, again.summary

    deleted = await svc.uninstall(pkg, actor_user_id=str(admin_user.id))
    assert deleted.get("agents") == 1
    assert await _agent(db, ns) is None


async def test_taking_over_an_identical_agent_still_records_it(db: AsyncSession, admin_user: User):
    """The content may already match (a hand edit undone, or one the package
    since adopted itself): the row still changes hands, visibly."""
    pkg, ns = _names()
    source = PACKAGE_YAML.format(pkg=pkg, ns=ns, prompt="Same prompt.")
    db.add(Agent(user_id=admin_user.id, namespace=ns, name="checker",
                 description="From the package", system_prompt="Same prompt."))
    await db.flush()

    plain, *_ = await PackageService(db).preview(source, str(admin_user.id))
    assert [c.action for c in plain.changes] == ["skipped"]

    _package, result = await PackageService(db).install(source, str(admin_user.id), adopt_unmanaged=True)

    [change] = result.changes
    assert change.action == "update" and change.reason == "adopted" and not change.changes
    [revision] = await _revisions(db, ns)
    assert revision.action == "update" and revision.managed_by == f"pkg:{pkg}"
    assert (await _agent(db, ns)).managed_by == f"pkg:{pkg}"


async def test_an_agent_of_another_package_stays_skipped_with_the_flag(db: AsyncSession, admin_user: User):
    pkg, ns = _names()
    db.add(Agent(user_id=admin_user.id, namespace=ns, name="checker", description="Theirs",
                 system_prompt="Another package's prompt.", managed_by="pkg:someone-else",
                 config_name="someone-else"))
    await db.flush()
    source = PACKAGE_YAML.format(pkg=pkg, ns=ns, prompt="Mine.")

    preview, *_ = await PackageService(db).preview(source, str(admin_user.id), adopt_unmanaged=True)
    _package, result = await PackageService(db).install(source, str(admin_user.id), adopt_unmanaged=True)

    for r in (preview, result):
        [change] = r.changes
        assert change.action == "skipped" and change.reason is None
        assert any("pkg:someone-else" in w for w in r.warnings), r.warnings
        assert not any("adoptUnmanaged" in w for w in r.warnings)
    agent = await _agent(db, ns)
    assert agent.system_prompt == "Another package's prompt." and agent.managed_by == "pkg:someone-else"


async def test_a_preview_with_the_flag_changes_nothing(db: AsyncSession, admin_user: User):
    pkg, ns = _names()
    source = await _hand_edited(db, admin_user, pkg, ns)
    before = len(await _revisions(db, ns))

    plain, *_ = await PackageService(db).preview(source, str(admin_user.id))
    adopting, *_ = await PackageService(db).preview(source, str(admin_user.id), adopt_unmanaged=True)

    assert [(c.action, c.reason) for c in plain.changes] == [("skipped", "edited_by_hand")]
    assert adopting.success and adopting.summary.updated == {"agents": 1}, adopting.summary
    assert [(c.action, c.reason) for c in adopting.changes] == [("update", "adopted")]
    assert any(
        "checker" in w and f"will be taken over by 'pkg:{pkg}'" in w
        and "uninstalling the package will delete it" in w
        for w in adopting.warnings
    ), adopting.warnings

    agent = await _agent(db, ns)
    assert agent.system_prompt == "A quick fix by hand." and agent.managed_by is None
    assert len(await _revisions(db, ns)) == before


async def test_the_endpoints_take_the_flag(client, db: AsyncSession, admin_user: User):
    pkg, ns = _names()
    source = await _hand_edited(db, admin_user, pkg, ns)
    h = auth_headers(admin_user)

    r = await client.post("/api/v1/packages/preview", json={"source": source, "adoptUnmanaged": True}, headers=h)
    assert r.status_code == 200, r.text
    assert [(c["action"], c["reason"]) for c in r.json()["changes"]] == [("update", "adopted")]

    r = await client.post("/api/v1/packages/install", json={"source": source, "adoptUnmanaged": True}, headers=h)
    assert r.status_code == 200, r.text
    assert (await _agent(db, ns)).managed_by == f"pkg:{pkg}"


def test_a_default_role_is_never_offered_or_taken():
    """Default roles are the platform's: their own ownership rule skips them
    before the flag is consulted, and a skip of one doesn't suggest it."""
    applier = RoleApplier()
    row = Role(name="Users", managed_by=None)
    ctx = ApplyContext(db=None, origin="package", managed_by="pkg:x", config_name="x")
    assert applier.ownership(row, ctx) == "skip"
    assert not applier._adoptable(row, ctx)
    ctx.adopt_unmanaged = True
    assert applier.ownership(row, ctx) == "skip"

    ordinary = Role(name=f"ops-{uuid.uuid4().hex[:6]}", managed_by=None)
    ctx.adopt_unmanaged = False
    assert applier._adoptable(ordinary, ctx)


async def test_a_hand_made_secret_keeps_its_value_and_outlives_the_package(db: AsyncSession, admin_user: User):
    """Secrets were never skipped (a package fills in its secret variables)
    and never deleted with a package; the flag changes neither. Declared
    without a value, the operator's stored value stays."""
    from app.core.encryption import encryption_service
    from app.models.secret import Secret

    pkg, name = f"pkg-{uuid.uuid4().hex[:8]}", f"TOK_{uuid.uuid4().hex[:6].upper()}"
    db.add(Secret(user_id=admin_user.id, name=name, visibility="shared",
                  encrypted_value=encryption_service.encrypt("by-hand")))
    await db.flush()
    source = f"""
apiVersion: sinas.co/v1
kind: SinasPackage
package:
  name: {pkg}
  version: "1.0.0"
spec:
  secrets:
    - name: {name}
      description: The package's token
"""
    svc = PackageService(db)
    _package, result = await svc.install(source, str(admin_user.id), adopt_unmanaged=True)
    assert result.success, result.errors
    assert not any("uninstalling" in w for w in result.warnings), result.warnings

    await svc.uninstall(pkg, actor_user_id=str(admin_user.id))
    secret = (
        await db.execute(select(Secret).where(Secret.name == name).execution_options(populate_existing=True))
    ).scalar_one()
    assert encryption_service.decrypt(secret.encrypted_value) == "by-hand"
