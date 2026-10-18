"""Chat retention admin API.

- GET  /retention/status: the policy, the archive destination (never its
  credentials), agent overrides, the last run, the run in progress, the
  next scheduled run. `sinas.retention.read:all`.
- POST /retention/run?dry_run=true: what a run would archive and delete now,
  per agent; changes nothing. `sinas.retention.read:all`.
- POST /retention/run?dry_run=false: queue a real run (202); its progress
  and result show in /retention/status. `sinas.retention.update:all`.

Admins have both through `sinas.*:all`; no other default role does.
See docs/design/chat-retention.md.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user_with_permissions, set_permission_used
from app.core.database import get_db
from app.core.permissions import check_permission
from app.models.retention_run import FAILED, QUEUED, ChatRetentionRun
from app.services.retention import job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/retention", tags=["retention"])

READ = "sinas.retention.read:all"
UPDATE = "sinas.retention.update:all"
RUN_JOB = "chat_retention_run_job"
AGENT_QUEUE = "sinas:queue:agents"


def _require(request: Request, permissions: dict, permission: str) -> None:
    if not check_permission(permissions, permission):
        set_permission_used(request, permission, has_perm=False)
        raise HTTPException(status_code=403, detail=f"Not authorized (needs {permission})")
    set_permission_used(request, permission)


@router.get("/status")
async def retention_status(
    request: Request,
    current_user_data: tuple = Depends(get_current_user_with_permissions),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _, permissions = current_user_data
    _require(request, permissions, READ)
    return await job.status(db)


@router.post("/run")
async def retention_run(
    request: Request,
    dry_run: bool = Query(True, description="Report what would be archived and deleted, without acting"),
    current_user_data: tuple = Depends(get_current_user_with_permissions),
    db: AsyncSession = Depends(get_db),
):
    user_id, permissions = current_user_data
    if dry_run:
        _require(request, permissions, READ)
        return await job.preview(db)

    _require(request, permissions, UPDATE)
    current = await job.status(db)
    if current["refusal"]:
        raise HTTPException(status_code=409, detail=current["refusal"])
    if current["in_progress"]:
        raise HTTPException(status_code=409, detail="A retention run is already queued or running")

    from app.models.user import User

    user = await db.get(User, uuid.UUID(str(user_id)))
    run = ChatRetentionRun(
        id=uuid.uuid4(),
        trigger="api",
        status=QUEUED,
        actor_user_id=uuid.UUID(str(user_id)),
        actor_email=user.email if user else None,
    )
    db.add(run)
    await db.commit()

    try:
        from app.core.redis import get_arq_pool

        pool = await get_arq_pool()
        # The job's timeout is the worker's (func(..., timeout=) in
        # AgentWorkerSettings): arq takes no per-enqueue timeout, and an
        # unknown _-kwarg would reach the job as an argument.
        await pool.enqueue_job(
            RUN_JOB,
            run_id=str(run.id),
            _job_id=f"chat-retention-{run.id}",
            _queue_name=AGENT_QUEUE,
        )
    except Exception as e:
        run.status = FAILED
        run.finished_at = datetime.now(UTC)
        run.error = f"Could not queue the run: {type(e).__name__}"
        await db.commit()
        logger.error("Chat retention: queueing run %s failed: %s", run.id, type(e).__name__)
        raise HTTPException(status_code=503, detail="Could not queue the retention run; try again") from None

    return JSONResponse(status_code=202, content={"run_id": str(run.id), "status": QUEUED})
