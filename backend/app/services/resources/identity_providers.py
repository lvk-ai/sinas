"""Identity providers applier (docs/design/oidc-sso.md).

Admin-managed, installation-wide (a name, no namespace), like LLM providers
and package repositories. Config files declare them; packages never do: a
package must not be able to add a way to sign in.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.models.identity_provider import IdentityProvider
from app.models.user import Role
from app.schemas.spec.identity_provider import ClaimMappingSpec, IdentityProviderSpec, RoleMappingSpec
from app.services.resources.base import ApplyContext, ReferenceNotFound, ResourceApplier


class IdentityProviderApplier(ResourceApplier[IdentityProviderSpec]):
    kind = "identityProviders"
    label = "Identity provider"
    noun = "identity provider"
    config_section = "identityProviders"
    spec_model = IdentityProviderSpec
    model = IdentityProvider
    # The roles it grants must exist when they change (a role deleted later
    # is skipped at login, with a warning).
    reference_fields = ("role_mappings", "default_role")
    # Switched off in the console is operator state a config keeps unless it
    # says otherwise.
    keep_unless_declared = ("is_active",)
    deleted_with_package = False

    def key_of(self, spec: IdentityProviderSpec) -> str:
        return spec.key

    def key_of_row(self, row: IdentityProvider) -> str:
        return row.name

    def config_key(self, item: Any) -> str:
        return item.name

    async def find(self, ctx: ApplyContext, key: str) -> IdentityProvider | None:
        return (
            await ctx.db.execute(
                select(IdentityProvider)
                .where(IdentityProvider.name == key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    def spec_from_row(self, row: IdentityProvider) -> IdentityProviderSpec:
        return IdentityProviderSpec.model_construct(
            name=row.name,
            display_name=row.display_name,
            issuer=row.issuer,
            client_id=row.client_id,
            client_secret=row.client_secret or None,
            scopes=list(row.scopes or []),
            email_claim=row.email_claim or "email",
            groups_claim=row.groups_claim or "groups",
            role_mappings=[
                RoleMappingSpec.model_construct(
                    group=rule.get("group"), email_domain=rule.get("email_domain"), roles=list(rule.get("roles") or [])
                )
                for rule in row.role_mappings or []
            ],
            claim_mappings=[
                ClaimMappingSpec.model_construct(
                    claim=m.get("claim"), field=m.get("field"),
                    target=m.get("target") if m.get("target") == "session" else None,
                )
                for m in row.claim_mappings or []
            ],
            allowed_domains=list(row.allowed_domains or []),
            auto_provision=bool(row.auto_provision),
            default_role=row.default_role or None,
            enforce_for_domains=list(row.enforce_for_domains or []),
            session_hours=row.session_hours or 12,
            is_active=row.is_active is not False,
        )

    def new_row(self, spec: IdentityProviderSpec, ctx: ApplyContext) -> IdentityProvider:
        owner = ctx.owner_user_id
        return IdentityProvider(user_id=uuid.UUID(str(owner)) if owner else None)

    def write_fields(self, row: IdentityProvider, spec: IdentityProviderSpec) -> None:
        row.name = spec.name
        row.display_name = spec.display_name
        row.issuer = spec.issuer
        row.client_id = spec.client_id
        row.client_secret = spec.client_secret
        row.scopes = list(spec.scopes)
        row.email_claim = spec.email_claim
        row.groups_claim = spec.groups_claim
        row.role_mappings = [rule.model_dump(mode="json") for rule in spec.role_mappings]
        row.claim_mappings = [m.model_dump(mode="json", exclude_none=True) for m in spec.claim_mappings]
        row.allowed_domains = list(spec.allowed_domains)
        row.auto_provision = spec.auto_provision
        row.default_role = spec.default_role
        row.enforce_for_domains = list(spec.enforce_for_domains)
        row.session_hours = spec.session_hours
        row.is_active = spec.is_active

    async def check_references(self, spec: IdentityProviderSpec, ctx: ApplyContext) -> None:
        wanted = spec.role_names()
        if not wanted:
            return
        found = set(
            (await ctx.db.execute(select(Role.name).where(Role.name.in_(wanted)))).scalars().all()
        )
        missing = sorted(name for name in wanted - found if not ctx.declared("roles", name))
        if missing:
            raise ReferenceNotFound(
                f"Identity provider '{spec.name}' grants roles that don't exist: {', '.join(missing)}"
            )
