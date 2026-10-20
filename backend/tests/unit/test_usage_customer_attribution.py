"""Usage attribution by customer, and the usage API's 0.5.0-rc.2 shape.

Pinned here: an llm_usage row records the customer (the user's
USAGE_CUSTOMER_FIELD custom field) of the call; the usage API groups and
filters by it (an opt-in `customer` dimension and filter); /usage/summary
(filtered by chat_id) and /usage/calls keep the shape and the read:own
scoping they had in 0.5.0-rc.2 — fields may be added, none renamed, removed
or restructured.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.models import LLMUsage
from tests.conftest import auth_headers
from tests.unit.test_usage_api import _get, _row, _user_with

# The response shapes of 0.5.0-rc.2 (git show 0.5.0-rc.2:backend/app/services/usage_reports.py).
RC2_METRICS = {
    "calls": int, "prompt_tokens": int, "completion_tokens": int, "total_tokens": int,
    "cache_read_tokens": int, "cache_write_tokens": int, "cost_usd": (float, int),
    "unpriced_calls": int, "errors": int,
}
RC2_SUMMARY = {"from", "to", "group_by", "totals", "groups", "truncated"}
RC2_CALLS = {"items", "limit", "offset", "has_more"}
RC2_CALL_ITEM = {
    "id", "created_at", "user_id", "chat_id", "message_id", "agent", "source", "provider_name",
    "provider_type", "model", "prompt_tokens", "completion_tokens", "total_tokens",
    "cache_read_tokens", "cache_write_tokens", "cost_usd", "price_source", "batch", "streamed",
    "latency_ms", "error",
}


@pytest.fixture
async def seeded(db):
    run_user = await _user_with(db, "sinas.usage.read:own")
    other = await _user_with(db, "sinas.usage.read:own")
    ns = f"c{uuid.uuid4().hex[:6]}"
    chat, other_chat = uuid.uuid4(), uuid.uuid4()
    db.add_all([
        _row(user_id=run_user.id, chat_id=chat, agent=f"{ns}/a", customer="org-1", cost_usd=Decimal("0.5")),
        _row(user_id=run_user.id, chat_id=chat, agent=f"{ns}/a", customer="org-1", cost_usd=Decimal("0.25")),
        _row(user_id=other.id, chat_id=other_chat, agent=f"{ns}/a", customer="org-2", cost_usd=Decimal("1")),
        _row(user_id=other.id, chat_id=other_chat, agent=f"{ns}/a", cost_usd=Decimal("2")),  # platform work
    ])
    await db.flush()
    return {"user": run_user, "other": other, "chat": chat, "other_chat": other_chat, "agent": f"{ns}/a"}


def _shaped(metrics: dict) -> None:
    assert set(metrics) >= set(RC2_METRICS)
    for key, kind in RC2_METRICS.items():
        assert isinstance(metrics[key], kind), key


async def test_summary_by_chat_keeps_the_rc2_shape_and_read_own_scope(client, seeded):
    body = await _get(client, seeded["user"], "summary", {"chat_id": str(seeded["chat"])})
    assert set(body) == RC2_SUMMARY
    assert body["from"] is None and body["group_by"] == [] and body["groups"] == [] and body["truncated"] is False
    _shaped(body["totals"])
    assert (body["totals"]["calls"], body["totals"]["cost_usd"]) == (2, 0.75)

    # read:own: another user's chat counts nothing, whatever is asked.
    body = await _get(client, seeded["user"], "summary", {"chat_id": str(seeded["other_chat"])})
    assert body["totals"]["calls"] == 0 and body["totals"]["cost_usd"] == 0
    body = await _get(client, seeded["user"], "summary", {"chat_id": str(seeded["other_chat"]), "customer": "org-2"})
    assert body["totals"]["calls"] == 0


async def test_calls_keep_the_rc2_shape_and_read_own_scope(client, seeded):
    body = await _get(client, seeded["user"], "calls", {"chat_id": str(seeded["chat"])})
    assert set(body) == RC2_CALLS
    assert len(body["items"]) == 2 and body["has_more"] is False
    for item in body["items"]:
        assert set(item) >= RC2_CALL_ITEM  # added fields only (user_email, customer)
        assert item["user_id"] == str(seeded["user"].id)
        assert item["customer"] == "org-1"
    body = await _get(client, seeded["user"], "calls", {"chat_id": str(seeded["other_chat"])})
    assert body["items"] == []


async def test_group_and_filter_by_customer(client, admin_user, seeded):
    params = {"agent": seeded["agent"], "group_by": "customer"}
    groups = (await _get(client, admin_user, "summary", params))["groups"]
    by_customer = {g["key"]["customer"]: (g["calls"], g["cost_usd"]) for g in groups}
    assert by_customer == {"org-1": (2, 0.75), "org-2": (1, 1.0), None: (1, 2.0)}

    body = await _get(client, admin_user, "summary", {"agent": seeded["agent"], "customer": ["org-1", "org-2"]})
    assert (body["totals"]["calls"], body["totals"]["cost_usd"]) == (3, 1.75)

    # read:own still scopes a customer filter to the caller's own calls.
    body = await _get(client, seeded["user"], "summary", {"agent": seeded["agent"], "customer": "org-2"})
    assert body["totals"]["calls"] == 0


async def test_the_customer_index_exists(db):
    rows = dict((await db.execute(text(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'llm_usage'"
    ))).all())
    assert "(customer, created_at)" in rows["idx_llm_usage_customer_created"]


async def test_a_call_records_its_customer(db, monkeypatch):
    """create_provider puts the customer in the usage context; the tracking
    wrapper writes it on the row."""
    from app.core.config import settings
    from app.models import LLMProvider
    from app.models.user import User
    from app.providers import create_provider, tracking

    rows = []

    async def insert(values):
        rows.append(values)
        return True

    monkeypatch.setattr(tracking, "_insert_usage", insert)
    monkeypatch.setattr(settings, "usage_customer_field", "orgId")
    user = User(email=f"cust-{uuid.uuid4().hex[:8]}@example.com", custom_fields={"orgId": "org-42"})
    row = LLMProvider(name=f"p-{uuid.uuid4().hex[:8]}", provider_type="ollama", api_endpoint="http://gateway.test", is_active=True)
    db.add_all([user, row])
    await db.flush()

    provider = await create_provider(row.name, "m", db, usage_context={"user_id": str(user.id), "source": "chat"})

    async def boom(**kwargs):
        raise RuntimeError("no network in tests")

    provider._inner.complete = lambda *a, **k: boom()
    with pytest.raises(RuntimeError):
        await provider.complete([{"role": "user", "content": "hi"}], model="m")
    import asyncio

    await asyncio.sleep(0)
    assert rows and rows[0]["customer"] == "org-42"
