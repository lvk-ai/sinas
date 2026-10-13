"""Package repositories applier (ADR 2026-10-13-package-repositories).

Admin-managed, installation-wide (a name, no namespace), like LLM providers.
Config files declare them; packages never do: a package must not be able to
add a source of further packages.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select

from app.models.package_repository import PackageRepository
from app.schemas.spec.package_repository import PackageRepositoryAuthSpec, PackageRepositorySpec
from app.services.resources.base import ApplyContext, ResourceApplier
from app.services.resources.mcp_servers import _redacted_url


@dataclass(frozen=True)
class PackageRepositoryRefresh:
    """Fetch a repository's index after the write that created or re-pointed
    it has committed (config apply, packages, restores; the REST endpoints
    fetch inline so their response shows the result)."""

    name: str

    async def run(self) -> None:
        from app.services.package_repositories import refresh_named

        await refresh_named(self.name)


class PackageRepositoryApplier(ResourceApplier[PackageRepositorySpec]):
    kind = "packageRepositories"
    label = "Package repository"
    noun = "package repository"
    config_section = "packageRepositories"
    spec_model = PackageRepositorySpec
    model = PackageRepository
    # Switched off in the console is operator state a config keeps unless it
    # says otherwise.
    keep_unless_declared = ("is_active",)
    deleted_with_package = False

    def key_of(self, spec: PackageRepositorySpec) -> str:
        return spec.key

    def key_of_row(self, row: PackageRepository) -> str:
        return row.name

    def config_key(self, item: Any) -> str:
        return item.name

    async def find(self, ctx: ApplyContext, key: str) -> PackageRepository | None:
        return (
            await ctx.db.execute(
                select(PackageRepository)
                .where(PackageRepository.name == key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    def spec_from_row(self, row: PackageRepository) -> PackageRepositorySpec:
        auth = {k: v for k, v in (row.auth or {}).items() if v is not None}
        auth.setdefault("type", "none")
        return PackageRepositorySpec.model_construct(
            name=row.name,
            description=row.description or None,
            url=row.url,
            auth=PackageRepositoryAuthSpec.model_construct(**auth),
            is_active=row.is_active is not False,
        )

    def new_row(self, spec: PackageRepositorySpec, ctx: ApplyContext) -> PackageRepository:
        owner = ctx.owner_user_id
        return PackageRepository(user_id=uuid.UUID(str(owner)) if owner else None)

    def write_fields(self, row: PackageRepository, spec: PackageRepositorySpec) -> None:
        auth = spec.auth.model_dump(mode="json", exclude_none=True)
        if row.url != spec.url or (row.auth or {}) != auth:
            # A different source (or credentials): the cached index described
            # the old one. The next refresh fetches it anew.
            row.index = None
            row.last_refreshed_at = None
            row.last_error = None
        row.name = spec.name
        row.description = spec.description
        row.url = spec.url
        row.auth = auth
        row.is_active = spec.is_active

    async def write_row(
        self, row: PackageRepository, spec: PackageRepositorySpec, ctx: ApplyContext,
        current: Optional[PackageRepositorySpec] = None,
    ) -> None:
        self.write_fields(row, spec)
        if ctx.origin != "api" and row.is_active and row.index is None:
            ctx.effects.add(PackageRepositoryRefresh(spec.name))

    # ---- history: no credentials --------------------------------------------

    def history_spec(self, spec: PackageRepositorySpec) -> dict[str, Any]:
        """A URL can carry credentials (https://<token>@host, ?token=...);
        history shows them redacted. `auth.secret` is a Secret's name."""
        state = spec.canonical()
        state["url"] = _redacted_url(spec.url)
        return state

    def secret_values(self, spec: PackageRepositorySpec) -> dict[str, Any]:
        return {"url": spec.url} if _redacted_url(spec.url) != spec.url else {}

    def with_secrets(self, state: dict[str, Any], secrets: dict[str, Any]) -> dict[str, Any]:
        state = dict(state)
        if "url" in secrets:
            state["url"] = secrets["url"]
        return state
