"""Reports over llm_usage: totals and breakdowns, time series, raw calls, and
the effective ratecard (GET /api/v1/usage/*, docs/design/usage-cost.md)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.llm_usage import LLMUsage

DEFAULT_DAYS = 30

# group_by dimension -> column. day/hour are UTC buckets. Literals, not bind
# parameters: Postgres matches a grouped expression to the selected one only
# when they are identical, and each bind parameter gets its own placeholder.
_UTC_CREATED = func.timezone(literal_column("'UTC'"), LLMUsage.created_at)
DIMENSIONS: dict[str, Any] = {
    "agent": LLMUsage.agent,
    "provider": LLMUsage.provider_name,
    "model": LLMUsage.model,
    "user": LLMUsage.user_id,
    "source": LLMUsage.source,
    "chat": LLMUsage.chat_id,
    "batch": LLMUsage.batch,
    "day": func.date_trunc(literal_column("'day'"), _UTC_CREATED),
    "hour": func.date_trunc(literal_column("'hour'"), _UTC_CREATED),
}
TIME_DIMENSIONS = ("day", "hour")


class ReportError(ValueError):
    """A request the reports can't answer (a 400)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class UsageFilters:
    """What a report covers. `own_user_id` is the read:own scope: set, only
    that user's calls are counted, whatever else is asked for."""

    start: Optional[datetime] = None
    end: Optional[datetime] = None
    agents: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    user_ids: list[uuid.UUID] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    chat_ids: list[uuid.UUID] = field(default_factory=list)
    own_user_id: Optional[uuid.UUID] = None

    def window(self, *, bounded: bool) -> tuple[Optional[datetime], datetime]:
        """[start, end). Without a `from`, the last DEFAULT_DAYS — except for a
        chat_id filter (`bounded` False): a chat's calls are found by its
        index whenever they were made."""
        end = self.end or _now()
        start = self.start
        if start is None and (bounded or not self.chat_ids):
            start = end - timedelta(days=DEFAULT_DAYS)
        if start is not None and start >= end:
            raise ReportError("'from' must be before 'to'")
        return start, end

    def clauses(self, *, bounded: bool = False) -> list[Any]:
        start, end = self.window(bounded=bounded)
        clauses: list[Any] = [LLMUsage.created_at < end]
        if start is not None:
            clauses.append(LLMUsage.created_at >= start)
        for column, values in (
            (LLMUsage.agent, self.agents),
            (LLMUsage.provider_name, self.providers),
            (LLMUsage.model, self.models),
            (LLMUsage.user_id, self.user_ids),
            (LLMUsage.source, self.sources),
            (LLMUsage.chat_id, self.chat_ids),
        ):
            if len(values) == 1:
                clauses.append(column == values[0])
            elif values:
                clauses.append(column.in_(values))
        if self.own_user_id is not None:
            clauses.append(LLMUsage.user_id == self.own_user_id)
        return clauses


def _metrics() -> list[Any]:
    return [
        func.count().label("calls"),
        func.coalesce(func.sum(LLMUsage.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(LLMUsage.completion_tokens), 0).label("completion_tokens"),
        func.coalesce(func.sum(LLMUsage.total_tokens), 0).label("total_tokens"),
        func.coalesce(func.sum(LLMUsage.cache_read_tokens), 0).label("cache_read_tokens"),
        func.coalesce(func.sum(LLMUsage.cache_write_tokens), 0).label("cache_write_tokens"),
        func.coalesce(func.sum(LLMUsage.cost_usd), 0).label("cost_usd"),
        func.count().filter(LLMUsage.cost_usd.is_(None)).label("unpriced_calls"),
        func.count().filter(LLMUsage.error.isnot(None)).label("errors"),
    ]


def money(value: Optional[Decimal]) -> Optional[float]:
    return None if value is None else float(value)


def _metric_values(row: Any) -> dict[str, Any]:
    return {
        "calls": int(row.calls),
        "prompt_tokens": int(row.prompt_tokens),
        "completion_tokens": int(row.completion_tokens),
        "total_tokens": int(row.total_tokens),
        "cache_read_tokens": int(row.cache_read_tokens),
        "cache_write_tokens": int(row.cache_write_tokens),
        "cost_usd": money(row.cost_usd),
        "unpriced_calls": int(row.unpriced_calls),
        "errors": int(row.errors),
    }


def _key_value(dimension: str, value: Any) -> Any:
    if value is None:
        return None
    if dimension in TIME_DIMENSIONS:
        return value.replace(tzinfo=timezone.utc).isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def parse_group_by(values: list[str]) -> list[str]:
    """group_by may repeat and/or be comma-separated."""
    dims: list[str] = []
    for value in values:
        for dim in value.split(","):
            dim = dim.strip()
            if not dim:
                continue
            if dim not in DIMENSIONS:
                raise ReportError(
                    f"Unknown group_by '{dim}'; one of: {', '.join(DIMENSIONS)}"
                )
            if dim not in dims:
                dims.append(dim)
    if len([d for d in dims if d in TIME_DIMENSIONS]) > 1:
        raise ReportError("group_by takes day or hour, not both")
    return dims


async def _user_emails(db: AsyncSession, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    from app.models.user import User

    rows = await db.execute(
        select(User.id, User.email).where(User.id.in_([uuid.UUID(i) for i in ids]))
    )
    return {str(user_id): email for user_id, email in rows}


async def summary(
    db: AsyncSession, filters: UsageFilters, group_by: list[str], limit: int
) -> dict[str, Any]:
    """Totals over the filters, and per group when grouped. Groups are
    ordered by time (when grouped by day/hour), then by cost and calls; at
    most `limit` of them (`truncated` says whether there were more)."""
    clauses = filters.clauses()
    start, end = filters.window(bounded=False)
    totals = (await db.execute(select(*_metrics()).where(*clauses))).one()
    result: dict[str, Any] = {
        "from": start.isoformat() if start else None,
        "to": end.isoformat(),
        "group_by": group_by,
        "totals": _metric_values(totals),
        "groups": [],
        "truncated": False,
    }
    if not group_by:
        return result

    columns = [DIMENSIONS[d].label(d) for d in group_by]
    order = [DIMENSIONS[d] for d in group_by if d in TIME_DIMENSIONS]
    order += [
        func.coalesce(func.sum(LLMUsage.cost_usd), 0).desc(),
        func.count().desc(),
        *(DIMENSIONS[d] for d in group_by if d not in TIME_DIMENSIONS),
    ]
    rows = (
        await db.execute(
            select(*columns, *_metrics())
            .where(*clauses)
            .group_by(*(DIMENSIONS[d] for d in group_by))
            .order_by(*order)
            .limit(limit + 1)
        )
    ).all()
    result["truncated"] = len(rows) > limit
    groups = []
    for row in rows[:limit]:
        key = {d: _key_value(d, getattr(row, d)) for d in group_by}
        groups.append({"key": key, **_metric_values(row)})
    if "user" in group_by:
        emails = await _user_emails(db, {g["key"]["user"] for g in groups if g["key"]["user"]})
        for g in groups:
            g["key"]["user_email"] = emails.get(g["key"]["user"])
    result["groups"] = groups
    return result


MAX_BUCKETS = 2000


async def timeseries(db: AsyncSession, filters: UsageFilters, interval: str) -> dict[str, Any]:
    """Metrics per UTC hour or day across the window, empty buckets included."""
    if interval not in TIME_DIMENSIONS:
        raise ReportError("interval is hour or day")
    start, end = filters.window(bounded=True)
    step = timedelta(hours=1) if interval == "hour" else timedelta(days=1)
    first = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    if interval == "day":
        first = first.replace(hour=0)
    if (end - first) / step > MAX_BUCKETS:
        raise ReportError(f"More than {MAX_BUCKETS} {interval} buckets; narrow the range")

    bucket = DIMENSIONS[interval]
    rows = (
        await db.execute(
            select(bucket.label("bucket"), *_metrics())
            .where(*filters.clauses(bounded=True))
            .group_by(bucket)
            .order_by(bucket)
        )
    ).all()
    by_bucket = {row.bucket.replace(tzinfo=timezone.utc): _metric_values(row) for row in rows}
    empty = {
        "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0,
        "unpriced_calls": 0, "errors": 0,
    }
    points = []
    at = first
    while at < end:
        points.append({"bucket": at.isoformat(), **by_bucket.get(at, empty)})
        at += step
    return {"from": start.isoformat(), "to": end.isoformat(), "interval": interval, "points": points}


async def calls(
    db: AsyncSession,
    filters: UsageFilters,
    *,
    limit: int,
    offset: int,
    unpriced_only: bool = False,
    errors_only: bool = False,
) -> dict[str, Any]:
    """Raw rows, newest first."""
    clauses = filters.clauses()
    if unpriced_only:
        clauses.append(LLMUsage.cost_usd.is_(None))
    if errors_only:
        clauses.append(LLMUsage.error.isnot(None))
    rows = (
        await db.execute(
            select(LLMUsage)
            .where(*clauses)
            .order_by(LLMUsage.created_at.desc(), LLMUsage.id.desc())
            .offset(offset)
            .limit(limit + 1)
        )
    ).scalars().all()
    items = [
        {
            "id": str(r.id),
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "user_id": str(r.user_id) if r.user_id else None,
            "chat_id": str(r.chat_id) if r.chat_id else None,
            "message_id": str(r.message_id) if r.message_id else None,
            "agent": r.agent,
            "source": r.source,
            "provider_name": r.provider_name,
            "provider_type": r.provider_type,
            "model": r.model,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "total_tokens": r.total_tokens,
            "cache_read_tokens": r.cache_read_tokens,
            "cache_write_tokens": r.cache_write_tokens,
            "cost_usd": money(r.cost_usd),
            "price_source": r.price_source,
            "batch": r.batch,
            "streamed": r.streamed,
            "latency_ms": r.latency_ms,
            "error": r.error,
        }
        for r in rows[:limit]
    ]
    return {"items": items, "limit": limit, "offset": offset, "has_more": len(rows) > limit}


# ---------------------------------------------------------------------------
# The effective ratecard
# ---------------------------------------------------------------------------


def _price_view(price: Any) -> dict[str, Any]:
    entry = price.to_entry()
    entry.pop("litellm_provider", None)
    return entry


async def ratecard(
    db: AsyncSession, *, days: int, search: Optional[str], limit: int
) -> dict[str, Any]:
    """What prices apply now and where each comes from: the deployment
    sources and the provider families they list (what a provider's
    pricing_provider can name), every provider's own pricing, each model
    called in the last `days` with the price it gets now (or none), and —
    with `search` — the deployment entries whose name contains it."""
    from app.models.llm_provider import LLMProvider
    from app.models.ratecard_cache import RatecardCache
    from app.services import llm_pricing
    from app.services.package_repositories import _safe

    deployment = await llm_pricing.deployment_ratecard(db, fresh=True)
    layers = llm_pricing.deployment_layers()
    cached = await db.get(RatecardCache, "url")
    sources = [
        {
            "source": "json",
            "configured": bool(settings.model_prices_json),
            "models": len(layers["json"].card),
            "error": layers["json"].error,
        },
        {
            "source": "file",
            "configured": bool(settings.model_prices_file),
            "path": settings.model_prices_file,
            "models": len(layers["file"].card),
            "error": layers["file"].error,
        },
        {
            "source": "url",
            "configured": bool(settings.model_prices_url),
            "url": _safe(settings.model_prices_url) if settings.model_prices_url else None,
            "models": len(layers["url"].card),
            "fetched_at": cached.fetched_at.isoformat() if cached and cached.fetched_at else None,
            "checked_at": cached.checked_at.isoformat() if cached and cached.checked_at else None,
            "error": layers["url"].error,
        },
    ]

    providers: dict[str, llm_pricing.ProviderPricing] = {}
    provider_views = []
    for row in (await db.execute(select(LLMProvider).order_by(LLMProvider.name))).scalars():
        pricing = llm_pricing.ProviderPricing.from_values(
            row.name, row.provider_type, row.pricing, row.model_aliases, row.pricing_discount,
            row.pricing_provider,
        )
        providers[row.name] = pricing
        provider_views.append({
            "name": row.name,
            "provider_type": row.provider_type,
            "is_active": row.is_active,
            "pricing": {model: _price_view(p) for model, p in sorted(pricing.prices.items())},
            "model_aliases": dict(row.model_aliases or {}),
            "discount": row.pricing_discount,
            "pricing_provider": pricing.pricing_provider,
            # The families its models are looked up under, set or mapped
            "priced_as_families": list(
                llm_pricing.lookup_families(row.provider_type, pricing.pricing_provider)
            ),
        })

    since = _now() - timedelta(days=days)
    seen = (
        await db.execute(
            select(
                LLMUsage.provider_name,
                LLMUsage.provider_type,
                LLMUsage.model,
                func.count().label("calls"),
                func.count().filter(LLMUsage.cost_usd.is_(None)).label("unpriced_calls"),
                func.max(LLMUsage.created_at).label("last_used"),
            )
            .where(LLMUsage.created_at >= since, LLMUsage.model.isnot(None))
            .group_by(LLMUsage.provider_name, LLMUsage.provider_type, LLMUsage.model)
            .order_by(func.count().desc())
        )
    ).all()
    in_use, unpriced = [], []
    for row in seen:
        provider = providers.get(row.provider_name) if row.provider_name else None
        resolution = llm_pricing.resolve(row.model, row.provider_type, provider, deployment)
        item = {
            "provider": row.provider_name,
            "provider_type": row.provider_type,
            "model": row.model,
            "calls": int(row.calls),
            "unpriced_calls": int(row.unpriced_calls),
            "last_used": row.last_used.isoformat() if row.last_used else None,
            "priced_as": resolution.priced_as if resolution else None,
            "matched_key": resolution.key if resolution else None,
            "source": resolution.source if resolution else None,
            "origin": resolution.price.origin if resolution else None,
            "discount": (
                float(resolution.multiplier)
                if resolution and resolution.source == "deployment" and resolution.multiplier != 1
                else None
            ),
            "price": _price_view(resolution.price) if resolution else None,
        }
        in_use.append(item)
        if resolution is None:
            unpriced.append(item)

    entries: list[dict[str, Any]] = []
    if search:
        needle = search.strip().lower()
        for key in sorted(deployment):
            if needle in key:
                price = deployment[key]
                entries.append({
                    "model": key, "origin": price.origin, "litellm_provider": price.provider,
                    "price": _price_view(price),
                })
                if len(entries) >= limit:
                    break

    return {
        "sources": sources,
        "deployment_models": len(deployment),
        "provider_families": [
            {"family": family, "models": count}
            for family, count in llm_pricing.provider_families(deployment).items()
        ],
        "providers": provider_views,
        "days": days,
        "models_in_use": in_use,
        "unpriced_models": unpriced,
        "deployment_entries": entries,
    }
