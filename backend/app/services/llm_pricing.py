"""LLM pricing: the ratecard, and what one call cost (docs/design/usage-cost.md).

Prices come from two places; the first that has the model wins:

1. The LLM provider's own `pricing` (after its `model_aliases`).
2. The deployment ratecard: MODEL_PRICES_JSON, MODEL_PRICES_FILE and
   MODEL_PRICES_URL, merged in that precedence. The URL is fetched by the
   worker cron and its last good copy kept in `ratecard_cache`.

Both use LiteLLM's model_prices_and_context_window.json format, so operators
can point at LiteLLM's maintained file. There are no bundled prices: a model
nobody priced is recorded with cost NULL and counted as unpriced.

A call is priced once, when its llm_usage row is written. A later price
change never rewrites what was recorded; POST /usage/backfill prices only
rows that were never priced.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

logger = logging.getLogger(__name__)

# LiteLLM's field name -> ModelPrice attribute. Everything else in an entry
# (context windows, capability flags, tiered prices) is ignored.
PRICE_FIELDS = {
    "input_cost_per_token": "input",
    "output_cost_per_token": "output",
    "cache_read_input_token_cost": "cache_read",
    "cache_creation_input_token_cost": "cache_write",
    "input_cost_per_token_batches": "input_batch",
    "output_cost_per_token_batches": "output_batch",
}

# Sinas provider type -> the litellm_provider names (and key prefixes) its
# models are listed under.
_LITELLM_PROVIDERS: dict[str, tuple[str, ...]] = {
    "openai": ("openai",),
    "azure": ("azure",),
    "azure_openai": ("azure",),
    "anthropic": ("anthropic",),
    "gemini": ("gemini",),
    "mistral": ("mistral",),
    "ollama": ("ollama",),
}

# A release date at the end of a model name: gpt-4o-2024-08-06,
# claude-3-5-sonnet-20241022, claude-3-5-sonnet@20241022 (Vertex).
_DATE_SUFFIX = re.compile(r"(?:-\d{4}-\d{2}-\d{2}|-\d{8}|@\d{8})$")

# cost_usd is Numeric(20, 10). Postgres rounds numeric half away from zero;
# costs are never negative, so HALF_UP rounds the same way.
_QUANTUM = Decimal("1e-10")
_ONE = Decimal(1)

DEPLOYMENT_LAYERS = ("json", "file", "url")  # highest precedence first


class RatecardError(Exception):
    """A ratecard that can't be used. The message is safe to show."""


# ---------------------------------------------------------------------------
# Ratecard entries
# ---------------------------------------------------------------------------


def _number(value: Any) -> Optional[Decimal]:
    """A per-token price, or None for a missing or unusable one."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value < 0:
            return None
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            return None
        return number if number.is_finite() and number >= 0 else None
    return None


@dataclass(frozen=True)
class ModelPrice:
    """Per-token USD prices for one model. `origin` says where it came from:
    "provider:<name>", "json", "file" or "url"."""

    input: Optional[Decimal] = None
    output: Optional[Decimal] = None
    cache_read: Optional[Decimal] = None
    cache_write: Optional[Decimal] = None
    input_batch: Optional[Decimal] = None
    output_batch: Optional[Decimal] = None
    provider: Optional[str] = None
    origin: str = ""

    def to_entry(self) -> dict[str, Any]:
        """The LiteLLM-shaped entry (only the fields set)."""
        entry: dict[str, Any] = {
            name: float(getattr(self, attr))
            for name, attr in PRICE_FIELDS.items()
            if getattr(self, attr) is not None
        }
        if self.provider:
            entry["litellm_provider"] = self.provider
        return entry


def parse_entry(entry: Any, origin: str) -> Optional[ModelPrice]:
    """One ratecard entry, or None when it prices neither input nor output
    (LiteLLM lists image, audio and search models that way)."""
    if not isinstance(entry, dict):
        return None
    values = {attr: _number(entry.get(name)) for name, attr in PRICE_FIELDS.items()}
    if values["input"] is None and values["output"] is None:
        return None
    provider = entry.get("litellm_provider")
    return ModelPrice(
        **values,
        provider=provider.strip().lower() if isinstance(provider, str) else None,
        origin=origin,
    )


def parse_ratecard(data: Any, origin: str) -> dict[str, ModelPrice]:
    """A LiteLLM-format ratecard (JSON text or the decoded object), keyed by
    lowercased model name. Entries without a usable price are skipped, as is
    LiteLLM's `sample_spec`."""
    if isinstance(data, (bytes, str)):
        try:
            data = json.loads(data)
        except ValueError as e:
            raise RatecardError(f"not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise RatecardError("a ratecard is a JSON object of model name -> prices")
    card: dict[str, ModelPrice] = {}
    for key, entry in data.items():
        if not isinstance(key, str) or not key.strip() or key == "sample_spec":
            continue
        price = parse_entry(entry, origin)
        if price is not None:
            card[key.strip().lower()] = price
    return card


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _names(model: str) -> list[str]:
    """The names to look a model up under: as called, then without a release
    date — only reached when the dated name has no entry of its own."""
    name = model.strip().lower()
    undated = _DATE_SUFFIX.sub("", name)
    return [name, undated] if undated and undated != name else [name]


def match(
    card: dict[str, ModelPrice], model: str, provider_type: Optional[str]
) -> Optional[tuple[str, ModelPrice]]:
    """(key, price) for `model` called through a provider of `provider_type`.

    Per name (see _names): the provider-qualified key first ("azure/gpt-4o",
    "gemini/gemini-2.0-flash"), then the bare name ("gpt-4o")."""
    if not model or not card:
        return None
    kind = (provider_type or "").strip().lower()
    prefixes = _LITELLM_PROVIDERS.get(kind, (kind,) if kind else ())
    for name in _names(model):
        for prefix in prefixes:
            key = f"{prefix}/{name}"
            if key in card:
                return key, card[key]
        if name in card:
            return name, card[name]
    return None


@dataclass(frozen=True)
class ProviderPricing:
    """An LLM provider's pricing settings."""

    name: str
    provider_type: Optional[str] = None
    prices: dict[str, ModelPrice] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)  # lowercased keys
    discount: Optional[Decimal] = None

    @classmethod
    def from_values(
        cls,
        name: str,
        provider_type: Optional[str],
        pricing: Optional[dict[str, Any]],
        model_aliases: Optional[dict[str, str]],
        discount: Optional[float],
    ) -> "ProviderPricing":
        return cls(
            name=name,
            provider_type=provider_type,
            prices=parse_ratecard(pricing or {}, f"provider:{name}"),
            aliases={
                str(k).strip().lower(): str(v).strip()
                for k, v in (model_aliases or {}).items()
                if str(k).strip() and str(v).strip()
            },
            discount=_number(discount),
        )


@dataclass(frozen=True)
class Resolution:
    """The price a call gets, and why."""

    price: ModelPrice
    key: str  # the ratecard key that matched
    source: str  # "provider" | "deployment"
    multiplier: Decimal  # the provider's discount, on deployment prices
    priced_as: str  # the model name after aliasing


def resolve(
    model: Optional[str],
    provider_type: Optional[str],
    provider: Optional[ProviderPricing],
    deployment: dict[str, ModelPrice],
) -> Optional[Resolution]:
    """The price for `model` as called through `provider`: the provider's
    alias (if any) replaces the name, then the provider's own pricing, then
    the deployment ratecard with the provider's discount applied. The
    discount is for list prices: a provider's own prices are taken as they
    are."""
    if not model:
        return None
    priced_as = model
    if provider is not None:
        priced_as = provider.aliases.get(model.strip().lower(), model)
        hit = match(provider.prices, priced_as, provider_type)
        if hit is not None:
            return Resolution(hit[1], hit[0], "provider", _ONE, priced_as)
    hit = match(deployment, priced_as, provider_type)
    if hit is None:
        return None
    multiplier = provider.discount if provider is not None and provider.discount is not None else _ONE
    return Resolution(hit[1], hit[0], "deployment", multiplier, priced_as)


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnitPrices:
    """The per-token prices one call is charged at; None = not priced."""

    input: Optional[Decimal]
    cache_read: Optional[Decimal]
    cache_write: Optional[Decimal]
    output: Optional[Decimal]


def unit_prices(price: ModelPrice, batch: bool) -> UnitPrices:
    """Cache tokens without a cache price are charged at the input price.
    A batch call uses the batch prices where the ratecard has them (and the
    regular price where it doesn't); its cache tokens get the same discount
    as its input tokens."""
    input_price, output_price = price.input, price.output
    cache_read = price.cache_read if price.cache_read is not None else price.input
    cache_write = price.cache_write if price.cache_write is not None else price.input
    if batch:
        if price.input_batch is not None:
            if price.input:
                ratio = price.input_batch / price.input
                cache_read = cache_read * ratio if cache_read is not None else None
                cache_write = cache_write * ratio if cache_write is not None else None
            input_price = price.input_batch
        if price.output_batch is not None:
            output_price = price.output_batch
    return UnitPrices(input_price, cache_read, cache_write, output_price)


def call_cost(
    price: ModelPrice,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    batch: bool = False,
    multiplier: Decimal = _ONE,
) -> Optional[Decimal]:
    """The USD cost of one call, or None when a token kind it used has no
    price.

    prompt_tokens includes the cache reads and writes (every provider's
    extract_usage normalizes to that), so only the rest is charged at the
    input price."""
    units = unit_prices(price, batch)
    uncached = max(prompt_tokens - cache_read_tokens - cache_write_tokens, 0)
    total = Decimal(0)
    for tokens, unit in (
        (uncached, units.input),
        (cache_read_tokens, units.cache_read),
        (cache_write_tokens, units.cache_write),
        (completion_tokens, units.output),
    ):
        if tokens <= 0:
            continue
        if unit is None:
            return None
        total += unit * tokens
    return (total * multiplier).quantize(_QUANTUM, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# The deployment ratecard
# ---------------------------------------------------------------------------

_CACHE_SECONDS = 60


@dataclass
class _Layer:
    key: Any = None  # what it was loaded from; reloaded when this changes
    card: dict[str, ModelPrice] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class _DeploymentCache:
    checked: float = 0.0
    layers: dict[str, _Layer] = field(default_factory=lambda: {n: _Layer() for n in DEPLOYMENT_LAYERS})
    merged: dict[str, ModelPrice] = field(default_factory=dict)
    merged_keys: Optional[dict[str, Any]] = None  # the layer keys `merged` was built from
    # One reload at a time: readers keep using `merged` meanwhile, and a
    # reload publishes a layer only once it is fully loaded.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # provider name -> (loaded at, its pricing or None)
    providers: dict[str, tuple[float, Optional["ProviderPricing"]]] = field(default_factory=dict)


_cache = _DeploymentCache()


def reset_cache() -> None:
    """Forget the loaded ratecard and provider pricing (tests; settings
    changed in-process)."""
    global _cache
    _cache = _DeploymentCache()


def _load_json_layer(layer: _Layer) -> None:
    raw = settings.model_prices_json
    if layer.key == ("json", raw):
        return
    card, error = {}, None
    if raw:
        try:
            card = parse_ratecard(raw, "json")
        except RatecardError as e:
            error = f"MODEL_PRICES_JSON: {e}"
            logger.warning("Ignoring the deployment ratecard: %s", error)
    layer.key, layer.card, layer.error = ("json", raw), card, error


def _load_file_layer(layer: _Layer) -> None:
    path = settings.model_prices_file
    try:
        mtime = os.stat(path).st_mtime if path else None
    except OSError:
        mtime = None
    if layer.key == ("file", path, mtime):
        return
    card, error = {}, None
    if path:
        try:
            with open(path, "rb") as f:
                raw = f.read(settings.model_prices_max_bytes + 1)
            if len(raw) > settings.model_prices_max_bytes:
                raise RatecardError(f"larger than {settings.model_prices_max_bytes} bytes")
            card = parse_ratecard(raw, "file")
        except (OSError, RatecardError) as e:
            error = f"MODEL_PRICES_FILE: {e}"
            logger.warning("Ignoring the deployment ratecard: %s", error)
    layer.key, layer.card, layer.error = ("file", path, mtime), card, error


async def _url_entries(db: AsyncSession) -> Optional[dict[str, Any]]:
    from app.models.ratecard_cache import RatecardCache

    return (
        await db.execute(select(RatecardCache.entries).where(RatecardCache.source == "url"))
    ).scalar_one_or_none()


async def _load_url_layer(db: AsyncSession, layer: _Layer) -> None:
    """Loads into locals and publishes key and card together, only once both
    queries succeeded: a failed load leaves the previous card (and key) in
    place, so the next reload tries again."""
    from app.models.ratecard_cache import RatecardCache

    url = settings.model_prices_url
    if not url:
        layer.key, layer.card, layer.error = None, {}, None
        return
    row = (
        await db.execute(
            select(RatecardCache.url, RatecardCache.fetched_at, RatecardCache.last_error).where(
                RatecardCache.source == "url"
            )
        )
    ).one_or_none()
    # Entries fetched from another URL than the one configured now are not
    # this ratecard.
    current = row is not None and row.url == url and row.fetched_at is not None
    key = ("url", url, row.fetched_at if current else None)
    error = row.last_error if row is not None else None
    if layer.key == key:
        layer.error = error
        return
    card: dict[str, ModelPrice] = {}
    if current:
        try:
            card = parse_ratecard(await _url_entries(db) or {}, "url")
        except RatecardError as e:  # pragma: no cover - stored entries were parsed once
            error = str(e)
    layer.key, layer.card, layer.error = key, card, error


async def deployment_ratecard(db: AsyncSession, *, fresh: bool = False) -> dict[str, ModelPrice]:
    """The merged deployment ratecard. Re-checked at most once a minute per
    process (the file by mtime, the URL copy by its fetch time), unless
    `fresh`. Reloads are serialised; a reload that fails raises and leaves
    the previous ratecard in use (and due again)."""
    if not fresh and time.monotonic() - _cache.checked < _CACHE_SECONDS:
        return _cache.merged
    cache = _cache
    async with cache.lock:
        now = time.monotonic()
        if not fresh and now - cache.checked < _CACHE_SECONDS:
            return cache.merged  # reloaded while this call waited
        layers = cache.layers
        _load_json_layer(layers["json"])
        _load_file_layer(layers["file"])
        await _load_url_layer(db, layers["url"])
        keys = {name: layer.key for name, layer in layers.items()}
        if keys != cache.merged_keys or not cache.checked:
            merged: dict[str, ModelPrice] = {}
            for name in reversed(DEPLOYMENT_LAYERS):  # lowest precedence first
                merged.update(layers[name].card)
            cache.merged, cache.merged_keys = merged, keys
        cache.checked = now
        return cache.merged


def deployment_layers() -> dict[str, _Layer]:
    """The layers as last loaded (for the ratecard view)."""
    return _cache.layers


# ---------------------------------------------------------------------------
# Pricing rows
# ---------------------------------------------------------------------------


async def provider_pricing(
    db: AsyncSession, name: Optional[str], *, fresh: bool = False
) -> Optional[ProviderPricing]:
    """The pricing of the provider named `name` (switched off or not), or
    None when there is no such provider. Cached per process for a minute
    like the deployment ratecard (so most calls price without a query),
    unless `fresh`."""
    if not name:
        return None
    cached = _cache.providers.get(name)
    if not fresh and cached is not None and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return cached[1]
    from app.models.llm_provider import LLMProvider

    row = (
        await db.execute(
            select(
                LLMProvider.name,
                LLMProvider.provider_type,
                LLMProvider.pricing,
                LLMProvider.model_aliases,
                LLMProvider.pricing_discount,
            ).where(LLMProvider.name == name)
        )
    ).one_or_none()
    pricing = None
    if row is not None:
        pricing = ProviderPricing.from_values(
            row.name, row.provider_type, row.pricing, row.model_aliases, row.pricing_discount
        )
    _cache.providers[name] = (time.monotonic(), pricing)
    return pricing


def _cached(provider_name: Optional[str]) -> bool:
    """Whether pricing a call through this provider needs no query now."""
    now = time.monotonic()
    if now - _cache.checked >= _CACHE_SECONDS:
        return False
    if not provider_name:
        return True
    cached = _cache.providers.get(provider_name)
    return cached is not None and now - cached[0] < _CACHE_SECONDS


async def price_usage(db: AsyncSession, values: dict[str, Any]) -> None:
    """Set cost_usd and price_source on an llm_usage row's values, in place,
    when a price is known. Never raises: a failure leaves the row unpriced
    (and backfillable).

    The pricing reads (made only when a cache is due) run in a savepoint of
    the caller's transaction: a query that fails or times out rolls back to
    it, so the usage row — and whatever else the caller writes, the batch
    poller's messages and executions — still commits."""
    name = values.get("provider_name")
    try:
        if _cached(name):  # the usual case: no query at all
            provider = await provider_pricing(db, name)
            deployment = await deployment_ratecard(db)
        else:
            async with db.begin_nested():
                provider = await provider_pricing(db, name)
                deployment = await deployment_ratecard(db)
    except Exception as e:
        logger.warning(f"Failed to price LLM usage: {e}")
        return
    try:
        resolution = resolve(values.get("model"), values.get("provider_type"), provider, deployment)
        if resolution is None:
            return
        cost = call_cost(
            resolution.price,
            prompt_tokens=values.get("prompt_tokens") or 0,
            completion_tokens=values.get("completion_tokens") or 0,
            cache_read_tokens=values.get("cache_read_tokens") or 0,
            cache_write_tokens=values.get("cache_write_tokens") or 0,
            batch=bool(values.get("batch")),
            multiplier=resolution.multiplier,
        )
        if cost is not None:
            values["cost_usd"] = cost
            values["price_source"] = resolution.source
    except Exception as e:
        logger.warning(f"Failed to price LLM usage: {e}")


async def backfill(
    db: AsyncSession, *, start: Optional[datetime] = None, end: Optional[datetime] = None
) -> dict[str, int]:
    """Price the rows in [start, end) that have no cost, with the prices that
    apply now; they get price_source "backfill". Rows already priced are
    never touched. One UPDATE per (provider, model, batch) group, committed
    as it goes, so a long range doesn't hold one big transaction.

    Without an `end`, the range ends when the backfill starts: calls
    recorded while it runs are left alone. The counts are what was priced
    and what is still unpriced in the range afterwards (examined is their
    sum), so they can't disagree whatever is written meanwhile."""
    from app.models.llm_usage import LLMUsage

    end = end or _now()
    window = [LLMUsage.cost_usd.is_(None), LLMUsage.created_at < end]
    if start is not None:
        window.append(LLMUsage.created_at >= start)

    groups = (
        await db.execute(
            select(
                LLMUsage.provider_name,
                LLMUsage.provider_type,
                LLMUsage.model,
                LLMUsage.batch,
                func.count(),
            )
            .where(*window, LLMUsage.model.isnot(None))
            .group_by(LLMUsage.provider_name, LLMUsage.provider_type, LLMUsage.model, LLMUsage.batch)
        )
    ).all()

    deployment = await deployment_ratecard(db, fresh=True)
    providers: dict[Optional[str], Optional[ProviderPricing]] = {}
    priced = 0
    for provider_name, provider_type, model, batch, _count in groups:
        if provider_name not in providers:
            providers[provider_name] = await provider_pricing(db, provider_name, fresh=True)
        resolution = resolve(model, provider_type, providers[provider_name], deployment)
        if resolution is None:
            continue
        units = unit_prices(resolution.price, batch)
        uncached = func.greatest(
            LLMUsage.prompt_tokens - LLMUsage.cache_read_tokens - LLMUsage.cache_write_tokens, 0
        )
        # Same arithmetic as call_cost: a token kind without a price leaves
        # the row unpriced unless the row has none of it.
        terms, conditions = [], []
        for tokens, unit in (
            (uncached, units.input),
            (LLMUsage.cache_read_tokens, units.cache_read),
            (LLMUsage.cache_write_tokens, units.cache_write),
            (LLMUsage.completion_tokens, units.output),
        ):
            if unit is None:
                conditions.append(tokens <= 0)
            else:
                terms.append(func.greatest(tokens, 0) * unit)
        cost = sum(terms[1:], terms[0]) * resolution.multiplier if terms else Decimal(0)
        result = await db.execute(
            update(LLMUsage)
            .where(
                *window,
                *conditions,
                LLMUsage.provider_name.is_not_distinct_from(provider_name),
                LLMUsage.provider_type.is_not_distinct_from(provider_type),
                LLMUsage.model == model,
                LLMUsage.batch.is_(batch),
            )
            .values(cost_usd=cost, price_source="backfill")
            .execution_options(synchronize_session=False)
        )
        priced += result.rowcount or 0
        await db.commit()
    unpriced = (await db.execute(select(func.count()).select_from(LLMUsage).where(*window))).scalar_one()
    return {"examined_calls": priced + unpriced, "priced_calls": priced, "unpriced_calls": unpriced}


# ---------------------------------------------------------------------------
# Fetching MODEL_PRICES_URL
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def fetch_url_ratecard(url: str) -> dict[str, ModelPrice]:
    """Fetch and parse the ratecard at `url`: https only, every redirect hop
    included, bounded in size and time (the package repository fetcher's
    hardening)."""
    from app.services.package_repositories import RepositoryError, _get

    try:
        raw = await _get(
            url,
            {},
            max_bytes=settings.model_prices_max_bytes,
            timeout=settings.model_prices_timeout_seconds,
            allow_http=False,
        )
    except RepositoryError as e:
        raise RatecardError(str(e)) from e
    card = parse_ratecard(raw, "url")
    if not card:
        raise RatecardError("the ratecard prices no model")
    return card


async def refresh_url_ratecard(db: AsyncSession, *, force: bool = False) -> bool:
    """Fetch MODEL_PRICES_URL into ratecard_cache when it is due (never
    fetched, older than model_prices_refresh_hours, or the URL changed), or
    always with `force`. A failure is recorded and the last good copy kept.
    True when a new copy was stored. Does not commit."""
    from app.models.ratecard_cache import RatecardCache
    from app.services.package_repositories import _safe

    url = settings.model_prices_url
    if not url:
        return False
    row = await db.get(RatecardCache, "url")
    if not force and row is not None and row.url == url and row.fetched_at is not None:
        if row.fetched_at > _now() - timedelta(hours=settings.model_prices_refresh_hours):
            return False

    # Nothing is locked while fetching.
    card, error = None, None
    try:
        card = await fetch_url_ratecard(url)
    except RatecardError as e:
        error = str(e)
        logger.warning("Model price ratecard %s: %s", _safe(url), error)

    # The first refresh creates the row. Two refreshes at once (an admin's
    # and the cron's) both insert-or-skip, then take turns on the row lock:
    # never a duplicate key.
    await db.execute(
        pg_insert(RatecardCache)
        .values(source="url", model_count=0)
        .on_conflict_do_nothing(index_elements=["source"])
    )
    row = await db.get(RatecardCache, "url", with_for_update=True, populate_existing=True)
    row.checked_at = _now()
    if card is None:
        row.last_error = error
        return False
    row.url = url
    row.entries = {key: price.to_entry() for key, price in card.items()}
    row.model_count = len(card)
    row.fetched_at = row.checked_at
    row.last_error = None
    return True


async def refresh_url_ratecard_due() -> bool:
    """The worker cron's entry point: refresh in a session of its own."""
    from app.core.database import AsyncSessionLocal

    if not settings.model_prices_url:
        return False
    async with AsyncSessionLocal() as db:
        stored = await refresh_url_ratecard(db)
        await db.commit()
    return stored
