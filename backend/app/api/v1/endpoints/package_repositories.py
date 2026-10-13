"""Package repository endpoints (ADR 2026-10-13-package-repositories).

Writes go through PackageRepositoryApplier, the path config apply uses too:
the same validation, ownership and change history on every channel. A
repository's index is fetched right after it is created or pointed
somewhere else, and on POST /{name}/refresh; the agent worker refreshes the
rest on a schedule.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_permission
from app.core.database import get_db
from app.models.package_repository import PackageRepository
from app.schemas.package_repository import (
    PackageRepositoryCreate,
    PackageRepositoryResponse,
    PackageRepositoryUpdate,
)
from app.services import package_repositories as repos
from app.services.resources import rest
from app.services.resources.package_repositories import PackageRepositoryApplier

router = APIRouter(prefix="/package-repositories", tags=["package-repositories"])

_applier = PackageRepositoryApplier()


async def _get(db: AsyncSession, name: str) -> PackageRepository:
    row = (await db.execute(select(PackageRepository).where(PackageRepository.name == name))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Package repository '{name}' not found")
    return row


async def _refreshed(db: AsyncSession, row: PackageRepository) -> PackageRepositoryResponse:
    """Fetch the index now; the outcome (or the error) is on the row."""
    await repos.refresh(db, row)
    await db.commit()
    await db.refresh(row)
    return PackageRepositoryResponse.of(row)


def _check_url(url: str) -> None:
    try:
        repos.base_url(url)
    except repos.RepositoryError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("", response_model=PackageRepositoryResponse, status_code=status.HTTP_201_CREATED)
async def create_package_repository(
    data: PackageRepositoryCreate,
    user_id: str = Depends(require_permission("sinas.package_repositories.create:all")),
    db: AsyncSession = Depends(get_db),
):
    """Add a package repository and fetch its index. Admin only."""
    _check_url(data.url)
    ctx = rest.api_context(db, user_id)
    result = await rest.write(_applier, ctx, rest.parse_spec(_applier, data.model_dump()), must_create=True)
    await rest.commit(db, ctx)
    await db.refresh(result.obj)
    return await _refreshed(db, result.obj)


@router.post("/official", response_model=PackageRepositoryResponse)
async def add_official_repository(
    user_id: str = Depends(require_permission("sinas.package_repositories.create:all")),
    db: AsyncSession = Depends(get_db),
):
    """Add the Sinas team's official repository (the console's quick add).
    Already added: returns it, refreshed."""
    existing = (
        await db.execute(
            select(PackageRepository).where(
                (PackageRepository.name == repos.OFFICIAL_REPOSITORY["name"])
                | (PackageRepository.url == repos.OFFICIAL_REPOSITORY["url"])
            )
        )
    ).scalars().first()
    if existing is not None:
        return await _refreshed(db, existing)
    ctx = rest.api_context(db, user_id)
    result = await rest.write(_applier, ctx, rest.parse_spec(_applier, dict(repos.OFFICIAL_REPOSITORY)), must_create=True)
    await rest.commit(db, ctx)
    await db.refresh(result.obj)
    return await _refreshed(db, result.obj)


@router.get("", response_model=list[PackageRepositoryResponse])
async def list_package_repositories(
    user_id: str = Depends(require_permission("sinas.package_repositories.read:all")),
    db: AsyncSession = Depends(get_db),
):
    """List package repositories with their refresh state. Admin only."""
    rows = (await db.execute(select(PackageRepository).order_by(PackageRepository.name))).scalars().all()
    return [PackageRepositoryResponse.of(r) for r in rows]


@router.get("/{name}", response_model=PackageRepositoryResponse)
async def get_package_repository(
    name: str,
    user_id: str = Depends(require_permission("sinas.package_repositories.read:all")),
    db: AsyncSession = Depends(get_db),
):
    return PackageRepositoryResponse.of(await _get(db, name))


@router.put("/{name}", response_model=PackageRepositoryResponse)
async def update_package_repository(
    name: str,
    data: PackageRepositoryUpdate,
    user_id: str = Depends(require_permission("sinas.package_repositories.update:all")),
    db: AsyncSession = Depends(get_db),
):
    """Update a repository. Pointed somewhere else (url or auth), its index
    is fetched anew."""
    row = await _get(db, name)
    patch = {k: v for k, v in data.model_dump().items() if v is not None}
    if "url" in patch:
        _check_url(patch["url"])
    ctx = rest.api_context(db, user_id)
    row = await rest.locked(_applier, ctx, row)
    await rest.write(_applier, ctx, rest.patch_spec(_applier, row, patch), existing=row)
    await rest.commit(db, ctx)
    await db.refresh(row)
    if row.is_active and row.index is None:
        return await _refreshed(db, row)
    return PackageRepositoryResponse.of(row)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_package_repository(
    name: str,
    user_id: str = Depends(require_permission("sinas.package_repositories.delete:all")),
    db: AsyncSession = Depends(get_db),
):
    """Remove a repository. Packages installed from it stay installed; they
    just no longer get update notices from it."""
    row = await _get(db, name)
    ctx = rest.api_context(db, user_id)
    await _applier.delete(await rest.locked(_applier, ctx, row), ctx)
    await rest.commit(db, ctx)
    return None


@router.post("/{name}/refresh", response_model=PackageRepositoryResponse)
async def refresh_package_repository(
    name: str,
    user_id: str = Depends(require_permission("sinas.package_repositories.update:all")),
    db: AsyncSession = Depends(get_db),
):
    """Fetch the repository's index now."""
    row = await _get(db, name)
    if not row.is_active:
        raise HTTPException(status_code=400, detail=f"Package repository '{name}' is disabled")
    return await _refreshed(db, row)
