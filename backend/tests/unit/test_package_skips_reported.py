"""A package install must not report a resource it left alone as unchanged.

An agent created by hand (managed_by NULL) is never adopted by a package
(design §4.4): the install skips it. That skip must be visible — counted as
skipped and explained in a warning — not folded into "unchanged", which
reads as "already matches the package" while the agent keeps old content.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.user import User
from app.services.package_service import PackageService

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
      systemPrompt: The package's prompt.
"""


async def test_a_hand_made_agent_is_reported_as_skipped(db: AsyncSession, admin_user: User):
    pkg, ns = f"pkg-{uuid.uuid4().hex[:8]}", f"ns{uuid.uuid4().hex[:6]}"
    db.add(Agent(user_id=admin_user.id, namespace=ns, name="checker", description="By hand",
                 system_prompt="An older prompt."))
    await db.flush()
    source = PACKAGE_YAML.format(pkg=pkg, ns=ns)

    preview, *_ = await PackageService(db).preview(source, str(admin_user.id))
    _package, result = await PackageService(db).install(source, str(admin_user.id))

    for r in (preview, result):
        assert r.summary.skipped == {"agents": 1} and not r.summary.unchanged, r.summary
        assert [c.action for c in r.changes] == ["skipped"]
        assert any("checker" in w and "by hand" in w for w in r.warnings), r.warnings

    agent = (await db.execute(select(Agent).where(Agent.namespace == ns, Agent.name == "checker"))).scalar_one()
    assert agent.system_prompt == "An older prompt." and agent.managed_by is None
