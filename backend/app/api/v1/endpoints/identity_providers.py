"""Identity provider endpoints (docs/design/oidc-sso.md).

Writes go through IdentityProviderApplier, the path config apply uses too:
the same validation, ownership and change history on every channel. The
login flow itself is public and lives in the runtime API (/auth/oidc).
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_permission
from app.core.database import get_db
from app.models.identity_provider import IdentityProvider
from app.schemas.identity_provider import (
    IdentityProviderCreate,
    IdentityProviderResponse,
    IdentityProviderUpdate,
)
from app.services.resources import rest
from app.services.resources.identity_providers import IdentityProviderApplier

router = APIRouter(prefix="/identity-providers", tags=["identity-providers"])

_applier = IdentityProviderApplier()

# Fields an update may clear by sending "" (or null).
_CLEARABLE = ("client_secret", "default_role")


async def _get(db: AsyncSession, name: str) -> IdentityProvider:
    row = (await db.execute(select(IdentityProvider).where(IdentityProvider.name == name))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Identity provider '{name}' not found")
    return row


@router.post("", response_model=IdentityProviderResponse, status_code=status.HTTP_201_CREATED)
async def create_identity_provider(
    data: IdentityProviderCreate,
    user_id: str = Depends(require_permission("sinas.identity_providers.create:all")),
    db: AsyncSession = Depends(get_db),
):
    """Add an identity provider. Admin only."""
    spec = {k: v for k, v in data.model_dump().items() if v is not None}
    ctx = rest.api_context(db, user_id)
    result = await rest.write(_applier, ctx, rest.parse_spec(_applier, spec), must_create=True)
    await rest.commit(db, ctx)
    await db.refresh(result.obj)
    return IdentityProviderResponse.of(result.obj)


@router.get("", response_model=list[IdentityProviderResponse])
async def list_identity_providers(
    user_id: str = Depends(require_permission("sinas.identity_providers.read:all")),
    db: AsyncSession = Depends(get_db),
):
    """List identity providers. Admin only."""
    rows = (await db.execute(select(IdentityProvider).order_by(IdentityProvider.name))).scalars().all()
    return [IdentityProviderResponse.of(r) for r in rows]


@router.get("/{name}", response_model=IdentityProviderResponse)
async def get_identity_provider(
    name: str,
    user_id: str = Depends(require_permission("sinas.identity_providers.read:all")),
    db: AsyncSession = Depends(get_db),
):
    return IdentityProviderResponse.of(await _get(db, name))


@router.put("/{name}", response_model=IdentityProviderResponse)
async def update_identity_provider(
    name: str,
    data: IdentityProviderUpdate,
    user_id: str = Depends(require_permission("sinas.identity_providers.update:all")),
    db: AsyncSession = Depends(get_db),
):
    """Update a provider: the fields sent, lists replaced whole."""
    row = await _get(db, name)
    patch = {
        k: (v if v != "" else None)
        for k, v in data.model_dump(exclude_unset=True, mode="json").items()
        if v is not None or k in _CLEARABLE
    }
    ctx = rest.api_context(db, user_id)
    row = await rest.locked(_applier, ctx, row)
    await rest.write(_applier, ctx, rest.patch_spec(_applier, row, patch), existing=row)
    await rest.commit(db, ctx)
    await db.refresh(row)
    return IdentityProviderResponse.of(row)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_identity_provider(
    name: str,
    user_id: str = Depends(require_permission("sinas.identity_providers.delete:all")),
    db: AsyncSession = Depends(get_db),
):
    """Remove a provider. Its users keep their accounts and the roles it
    granted (until an admin changes them); they sign in another way."""
    row = await _get(db, name)
    ctx = rest.api_context(db, user_id)
    await _applier.delete(await rest.locked(_applier, ctx, row), ctx)
    await rest.commit(db, ctx)
    return None
