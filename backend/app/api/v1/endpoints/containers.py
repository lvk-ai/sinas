"""Sandbox container management endpoints."""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_permission
from app.core.database import get_db
from app.schemas.container import ScaleRequest
from app.services.container_pool import container_pool

router = APIRouter(prefix="/containers", tags=["containers"])


@router.get("/stats")
async def get_container_stats(
    user_id: str = Depends(require_permission("sinas.system.read:all")),
) -> dict[str, Any]:
    """Get sandbox container stats. Admin only."""

    return container_pool.get_stats()


@router.post("/reload")
async def reload_pool_packages(
    current_user_id: str = Depends(require_permission("sinas.system.update:all")),
    db: AsyncSession = Depends(get_db),
):
    """
    Apply approved-package changes to the sandbox.

    - docker_pool: reinstall packages in idle containers and restart them.
    - docker_ephemeral: rebuild the baked sandbox image (containers are
      per-execution, so there is nothing to reload).
    Admin only.
    """
    from app.core.config import settings

    if settings.sandbox_executor == "docker_ephemeral":
        from app.services.sandbox_image import build_sandbox_image

        tag = await build_sandbox_image(db)
        return {"status": "rebuilt", "image": tag}

    result = await container_pool.reload_packages(db)
    return result


@router.post("/scale")
async def scale_pool(
    body: ScaleRequest,
    current_user_id: str = Depends(require_permission("sinas.system.update:all")),
    db: AsyncSession = Depends(get_db),
):
    """Scale the sandbox containers to a target size. Admin only."""

    if body.target < 0:
        raise HTTPException(status_code=400, detail="Target must be non-negative")

    result = await container_pool.scale(body.target, db)
    return result
