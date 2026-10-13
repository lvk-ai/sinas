"""Package secret-type variables: user attribution, preview purity, scoping.

Field report from integration-package testing: any package declaring a
`type: secret` variable 500'd on preview AND install with a NOT NULL
violation (secrets.user_id) — 5 of 6 integration packages uninstallable.
Preview also performed the INSERT, so a dry run persisted secrets.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encryption_service
from app.models.secret import Secret
from app.services.package_service import PackageService

YAML = """
kind: SinasPackage
metadata:
  name: test-secret-pkg
spec:
  variables:
    - name: {name}
      type: secret
      description: token
  connectors:
    - namespace: default
      name: probe
      baseUrl: https://example.com
      auth:
        type: bearer
        token: ${{{{ vars.{name} }}}}
"""


class TestPackageSecretVariables:
    async def test_install_path_creates_shared_secret_with_owner(
        self, db: AsyncSession, test_user
    ):
        name = f"TOK_{uuid.uuid4().hex[:6].upper()}"
        svc = PackageService(db)
        substituted, stored = await svc._resolve_variables(
            YAML.format(name=name), {name: "s3cret"}, str(test_user.id)
        )
        row = (
            await db.execute(select(Secret).where(Secret.name == name))
        ).scalar_one()
        assert str(row.user_id) == str(test_user.id)  # was NULL -> 500
        assert row.visibility == "shared"
        assert encryption_service.decrypt(row.encrypted_value) == "s3cret"
        assert stored[name] == "***"
        assert f"{{{{{name}}}}}" in substituted

    async def test_preview_persists_nothing(self, db: AsyncSession, test_user):
        name = f"TOK_{uuid.uuid4().hex[:6].upper()}"
        svc = PackageService(db)
        substituted, stored = await svc._resolve_variables(
            YAML.format(name=name), {name: "s3cret"}, str(test_user.id),
            persist_secrets=False,
        )
        assert (
            await db.execute(select(Secret).where(Secret.name == name))
        ).scalar_one_or_none() is None  # dry run wrote a secret before
        # Substitution still behaves identically
        assert f"{{{{{name}}}}}" in substituted
        assert stored[name] == "***"

    async def test_private_secret_of_other_user_not_overwritten(
        self, db: AsyncSession, test_user, admin_user
    ):
        name = f"TOK_{uuid.uuid4().hex[:6].upper()}"
        private = Secret(
            user_id=test_user.id,
            name=name,
            encrypted_value=encryption_service.encrypt("private-value"),
            visibility="private",
        )
        db.add(private)
        await db.flush()

        svc = PackageService(db)
        await svc._resolve_variables(
            YAML.format(name=name), {name: "package-value"}, str(admin_user.id)
        )
        await db.refresh(private)
        assert encryption_service.decrypt(private.encrypted_value) == "private-value"
        shared = (
            await db.execute(
                select(Secret).where(
                    Secret.name == name, Secret.visibility == "shared"
                )
            )
        ).scalar_one()
        assert encryption_service.decrypt(shared.encrypted_value) == "package-value"

    async def test_existing_shared_secret_updated_in_place(
        self, db: AsyncSession, test_user
    ):
        name = f"TOK_{uuid.uuid4().hex[:6].upper()}"
        svc = PackageService(db)
        await svc._resolve_variables(YAML.format(name=name), {name: "one"}, str(test_user.id))
        await svc._resolve_variables(YAML.format(name=name), {name: "two"}, str(test_user.id))
        rows = (
            await db.execute(select(Secret).where(Secret.name == name))
        ).scalars().all()
        assert len(rows) == 1
        assert encryption_service.decrypt(rows[0].encrypted_value) == "two"


def _skill_package(pkg: str, variables: str, content: str) -> str:
    return (
        "apiVersion: sinas.co/v1\nkind: SinasPackage\n"
        f"package:\n  name: {pkg}\n  version: \"1.0.0\"\n"
        f"spec:\n  variables:\n{variables}"
        "  skills:\n"
        f"    - namespace: {pkg}\n      name: hello\n      description: Says hello\n"
        f"      content: \"{content}\"\n"
    )


class TestPreviewAppliesTheInstallRulesForVariables:
    """The preview resolves variables exactly as the install does, also with
    no values given, so a successful preview is the console's only gate."""

    async def test_an_existing_secret_need_not_be_entered_again(self, db: AsyncSession, admin_user):
        name = f"TOK_{uuid.uuid4().hex[:6].upper()}"
        db.add(Secret(
            user_id=admin_user.id, name=name,
            encrypted_value=encryption_service.encrypt("kept"), visibility="shared",
        ))
        await db.flush()
        pkg = f"p-{uuid.uuid4().hex[:8]}"
        yaml_text = _skill_package(
            pkg, f"    - {{name: {name}, type: secret, required: true}}\n", f"Token ${{{{ vars.{name} }}}}."
        )
        result, *_ = await PackageService(db).preview(yaml_text, str(admin_user.id))
        assert result.success, result.errors

    async def test_a_required_value_with_an_empty_default_is_not_missing(self, db: AsyncSession, admin_user):
        pkg = f"p-{uuid.uuid4().hex[:8]}"
        yaml_text = _skill_package(
            pkg, "    - {name: SUFFIX, type: text, required: true, default: ''}\n", "Hello${{ vars.SUFFIX }}."
        )
        result, *_ = await PackageService(db).preview(yaml_text, str(admin_user.id))
        assert result.success, result.errors

    async def test_a_missing_required_value_is_reported_by_the_preview(self, db: AsyncSession, admin_user):
        pkg = f"p-{uuid.uuid4().hex[:8]}"
        yaml_text = _skill_package(
            pkg, "    - {name: GREETING, type: text, required: true}\n", "Say ${{ vars.GREETING }}."
        )
        result, declarations, requires_input, _ = await PackageService(db).preview(yaml_text, str(admin_user.id))
        assert not result.success and "GREETING" in result.errors[0]
        assert requires_input and [d["name"] for d in declarations] == ["GREETING"]
        result, *_ = await PackageService(db).preview(yaml_text, str(admin_user.id), variables={"GREETING": "hi"})
        assert result.success, result.errors
