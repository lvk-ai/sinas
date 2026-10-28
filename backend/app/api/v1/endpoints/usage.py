"""LLM usage and cost (docs/design/usage-cost.md).

Reads llm_usage: one row per LLM call, priced when it was written.

Permissions:
- sinas.usage.read:all  — everyone's calls, and the ratecard
- sinas.usage.read:own  — only the caller's own calls, whatever the filters
- sinas.usage.update:all — refresh the URL ratecard, backfill unpriced rows
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import AliasChoices, BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user_with_permissions, require_permission, set_permission_used
from app.core.database import get_db
from app.core.permissions import check_permission
from app.services import llm_pricing, usage_reports
from app.services.usage_reports import ReportError, UsageFilters

router = APIRouter(prefix="/usage", tags=["usage"])

# A chat_id filter goes in the query string: bounded so the URL stays sane.
MAX_CHAT_IDS = 500


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    """Query datetimes without an offset are UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _scope(request: Request, current_user_data: tuple) -> Optional[uuid.UUID]:
    """None for read:all; the caller's id for read:own (every query is then
    limited to it); 403 otherwise."""
    user_id, permissions = current_user_data
    if check_permission(permissions, "sinas.usage.read:all"):
        set_permission_used(request, "sinas.usage.read:all")
        return None
    if check_permission(permissions, "sinas.usage.read:own"):
        set_permission_used(request, "sinas.usage.read:own")
        return uuid.UUID(str(user_id))
    set_permission_used(request, "sinas.usage.read:own", has_perm=False)
    raise HTTPException(status_code=403, detail="Not authorized to view LLM usage")


def _filters(
    request: Request,
    start: Optional[datetime] = Query(
        None, alias="from", description="Start (inclusive). Default: 30 days before 'to' (none with chat_id)"
    ),
    end: Optional[datetime] = Query(None, alias="to", description="End (exclusive). Default: now"),
    agent: list[str] = Query([], description="Agent 'namespace/name' (repeatable)"),
    provider: list[str] = Query([], description="LLM provider name (repeatable)"),
    model: list[str] = Query([], description="Model as called (repeatable)"),
    user_id: list[uuid.UUID] = Query([], description="User id (repeatable)"),
    source: list[str] = Query([], description="Call site, e.g. chat, provider_batch (repeatable)"),
    chat_id: list[uuid.UUID] = Query([], description="Chat id (repeatable)"),
    customer: list[str] = Query(
        [], description="Customer, the USAGE_CUSTOMER_FIELD custom field (repeatable)"
    ),
    current_user_data: tuple = Depends(get_current_user_with_permissions),
) -> UsageFilters:
    own = _scope(request, current_user_data)
    if len(chat_id) > MAX_CHAT_IDS:
        raise HTTPException(status_code=400, detail=f"At most {MAX_CHAT_IDS} chat_id values")
    return UsageFilters(
        start=_utc(start),
        end=_utc(end),
        agents=agent,
        providers=provider,
        models=model,
        user_ids=user_id,
        sources=source,
        chat_ids=chat_id,
        customers=customer,
        own_user_id=own,
    )


@router.get("/summary")
async def usage_summary(
    group_by: list[str] = Query(
        [],
        description="Any of agent, provider, model, user, source, chat, batch, customer, and day or hour; "
        "repeatable or comma-separated",
    ),
    limit: int = Query(1000, ge=1, le=10000, description="Max groups"),
    filters: UsageFilters = Depends(_filters),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Calls, tokens, cost, unpriced calls and errors: totals, and per group
    when grouped."""
    try:
        return await usage_reports.summary(db, filters, usage_reports.parse_group_by(group_by), limit)
    except ReportError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/timeseries")
async def usage_timeseries(
    interval: str = Query("day", description="hour or day (UTC buckets)"),
    filters: UsageFilters = Depends(_filters),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The same metrics per hour or day, empty buckets included. The window
    defaults to the last 30 days, chat_id filter or not."""
    try:
        return await usage_reports.timeseries(db, filters, interval)
    except ReportError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/calls")
async def usage_calls(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    unpriced_only: bool = Query(False),
    errors_only: bool = Query(False),
    filters: UsageFilters = Depends(_filters),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Individual calls, newest first, for drill-down."""
    try:
        return await usage_reports.calls(
            db, filters, limit=limit, offset=offset,
            unpriced_only=unpriced_only, errors_only=errors_only,
        )
    except ReportError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/ratecard")
async def usage_ratecard(
    days: int = Query(30, ge=1, le=366, description="Window for the models seen in usage"),
    search: Optional[str] = Query(None, description="List deployment entries whose name contains this"),
    limit: int = Query(200, ge=1, le=2000, description="Max deployment entries listed"),
    user_id: str = Depends(require_permission("sinas.usage.read:all")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The effective prices and where each comes from: deployment sources,
    provider pricing, the models in use with their current price, and the
    models in use that have none."""
    return await usage_reports.ratecard(db, days=days, search=search, limit=limit)


@router.post("/ratecard/refresh")
async def refresh_ratecard(
    user_id: str = Depends(require_permission("sinas.usage.update:all")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Fetch MODEL_PRICES_URL now. A failure keeps the last good copy."""
    from app.core.config import settings
    from app.models.ratecard_cache import RatecardCache

    if not settings.model_prices_url:
        raise HTTPException(status_code=400, detail="MODEL_PRICES_URL is not set")
    stored = await llm_pricing.refresh_url_ratecard(db, force=True)
    await db.commit()
    row = await db.get(RatecardCache, "url")
    return {
        "refreshed": stored,
        "models": row.model_count if row else 0,
        "fetched_at": row.fetched_at.isoformat() if row and row.fetched_at else None,
        "error": row.last_error if row else None,
    }


class BackfillRequest(BaseModel):
    start: Optional[datetime] = Field(None, validation_alias=AliasChoices("from", "start"))
    end: Optional[datetime] = Field(None, validation_alias=AliasChoices("to", "end"))


@router.post("/backfill")
async def backfill_costs(
    request: Optional[BackfillRequest] = None,
    user_id: str = Depends(require_permission("sinas.usage.update:all")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Price calls recorded without a cost (`from` inclusive, `to` exclusive;
    both optional) at the prices that apply now. They get price_source
    "backfill"; calls already priced are never changed."""
    request = request or BackfillRequest()
    start, end = _utc(request.start), _utc(request.end)
    if start and end and start >= end:
        raise HTTPException(status_code=400, detail="start must be before end")
    return await llm_pricing.backfill(db, start=start, end=end)
