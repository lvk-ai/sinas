"""Runtime endpoints for batch polling, drill-in, and cancellation."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user_with_permissions
from app.core.database import get_db
from app.models.execution import ExecutionStatus
from app.schemas.batch import (
    BatchCancelResponse,
    BatchExecutionsResponse,
    BatchStatusResponse,
)
from app.services import batch_service

router = APIRouter()


@router.get("/batches/{batch_id}", response_model=BatchStatusResponse)
async def get_batch(
    batch_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user_data: tuple = Depends(get_current_user_with_permissions),
):
    """Aggregate progress + per-status counts for a batch."""
    user_id, _ = current_user_data
    result = await batch_service.get_batch_status(db=db, batch_id=batch_id, user_id=user_id)
    if result is None:
        raise HTTPException(404, "Batch not found")
    return result


@router.get(
    "/batches/{batch_id}/executions",
    response_model=BatchExecutionsResponse,
)
async def list_batch_executions(
    batch_id: str,
    request: Request,
    status: Optional[str] = Query(None, description="Filter by execution status"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user_data: tuple = Depends(get_current_user_with_permissions),
):
    """List child executions of a batch, optionally filtered by status."""
    user_id, _ = current_user_data
    status_enum = None
    if status:
        try:
            status_enum = ExecutionStatus(status.upper())
        except ValueError:
            raise HTTPException(400, f"Invalid status: {status}")
    rows = await batch_service.list_batch_executions(
        db=db,
        batch_id=batch_id,
        user_id=user_id,
        status=status_enum,
        limit=limit,
        offset=offset,
    )
    if rows is None:
        raise HTTPException(404, "Batch not found")
    return {"executions": rows}


@router.post("/batches/{batch_id}/cancel", response_model=BatchCancelResponse)
async def cancel_batch(
    batch_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user_data: tuple = Depends(get_current_user_with_permissions),
):
    """Cancel a batch: any queued/pending children become CANCELLED.

    Running children must complete naturally; their results still count
    toward the batch aggregate.
    """
    user_id, _ = current_user_data
    result = await batch_service.cancel_batch(db=db, batch_id=batch_id, user_id=user_id)
    if result is None:
        raise HTTPException(404, "Batch not found")
    return result
