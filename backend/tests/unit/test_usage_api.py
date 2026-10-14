"""GET /api/v1/usage/*: totals and breakdowns, time series, raw calls and
the ratecard; read:own sees only its own calls whatever the filters say."""
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.models import LLMUsage
from app.models.llm_provider import LLMProvider
from app.models.user import Role, RolePermission, User, UserRole
from app.services import llm_pricing
from tests.conftest import auth_headers


@pytest.fixture(autouse=True)
def _clean_ratecard(monkeypatch):
    for name in ("model_prices_json", "model_prices_file", "model_prices_url"):
        monkeypatch.setattr(settings, name, None)
    llm_pricing.reset_cache()
    yield
    llm_pricing.reset_cache()


async def _user_with(db, *permissions: str) -> User:
    role = Role(name=f"usage-{uuid.uuid4().hex[:8]}", description="usage test")
    db.add(role)
    await db.flush()
    for key in permissions:
        db.add(RolePermission(role_id=role.id, permission_key=key, permission_value=True))
    user = User(email=f"usage-{uuid.uuid4().hex[:8]}@example.com")
    db.add(user)
    await db.flush()
    db.add(UserRole(role_id=role.id, user_id=user.id, active=True))
    await db.flush()
    return user


def _row(**values) -> LLMUsage:
    defaults = {
        "provider_name": "p", "provider_type": "openai", "model": "m", "source": "chat",
        "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
        "cache_read_tokens": 0, "cache_write_tokens": 0, "streamed": False,
        "created_at": datetime.now(timezone.utc) - timedelta(hours=1),
    }
    return LLMUsage(**{**defaults, **values})


@pytest.fixture
async def seeded(db, admin_user):
    """Calls of two users under a unique agent namespace, so filtering by
    those agents isolates them from whatever else the database holds."""
    ns = f"u{uuid.uuid4().hex[:6]}"
    other = await _user_with(db, "sinas.usage.read:own")
    now = datetime.now(timezone.utc)
    chat_a, chat_b = uuid.uuid4(), uuid.uuid4()
    rows = [
        _row(user_id=admin_user.id, chat_id=chat_a, agent=f"{ns}/a", model="gpt-x", cost_usd=Decimal("0.5"), price_source="deployment"),
        _row(user_id=admin_user.id, chat_id=chat_a, agent=f"{ns}/a", model="gpt-x", cost_usd=Decimal("0.25"), price_source="deployment",
             created_at=now - timedelta(days=1, hours=1)),
        _row(user_id=admin_user.id, chat_id=chat_b, agent=f"{ns}/b", model="free-y", error="boom"),
        _row(user_id=other.id, chat_id=uuid.uuid4(), agent=f"{ns}/a", model="gpt-x", cost_usd=Decimal("1"), price_source="provider",
             provider_name="q", cache_read_tokens=40, batch=True, source="provider_batch"),
        # Outside the default 30-day window
        _row(user_id=admin_user.id, chat_id=chat_a, agent=f"{ns}/a", model="gpt-x", cost_usd=Decimal("100"),
             created_at=now - timedelta(days=40)),
    ]
    db.add_all(rows)
    await db.flush()
    return {"ns": ns, "other": other, "chat_a": chat_a, "chat_b": chat_b, "agents": [f"{ns}/a", f"{ns}/b"]}


def _q(seeded, **params) -> dict:
    return {"agent": seeded["agents"], **params}


async def _get(client, user, path, params) -> dict:
    r = await client.get(f"/api/v1/usage/{path}", params=params, headers=auth_headers(user))
    assert r.status_code == 200, r.text
    return r.json()


class TestSummary:
    async def test_totals(self, client, admin_user, seeded):
        body = await _get(client, admin_user, "summary", _q(seeded))
        assert body["totals"] == {
            "calls": 4, "prompt_tokens": 400, "completion_tokens": 40, "total_tokens": 440,
            "cache_read_tokens": 40, "cache_write_tokens": 0, "cost_usd": 1.75,
            "unpriced_calls": 1, "errors": 1,
        }
        assert body["groups"] == []

    async def test_group_by_combines_and_orders_by_cost(self, client, admin_user, seeded):
        body = await _get(client, admin_user, "summary", _q(seeded, group_by=["agent,model", "user"]))
        keys = [(g["key"]["agent"], g["key"]["model"], g["key"]["user_email"], g["cost_usd"]) for g in body["groups"]]
        ns = seeded["ns"]
        assert keys == [
            (f"{ns}/a", "gpt-x", seeded["other"].email, 1.0),
            (f"{ns}/a", "gpt-x", admin_user.email, 0.75),
            (f"{ns}/b", "free-y", admin_user.email, 0.0),
        ]
        assert body["groups"][2]["unpriced_calls"] == 1 and body["groups"][2]["errors"] == 1

        for dim, expected in (
            ("provider", {"p": 3, "q": 1}),
            ("source", {"chat": 3, "provider_batch": 1}),
            ("batch", {False: 3, True: 1}),
            ("chat", {str(seeded["chat_a"]): 2, str(seeded["chat_b"]): 1}),
        ):
            groups = (await _get(client, admin_user, "summary", _q(seeded, group_by=dim)))["groups"]
            counts = {g["key"][dim]: g["calls"] for g in groups}
            assert {k: v for k, v in counts.items() if k in expected} == expected

    async def test_day_buckets_are_utc_and_first(self, client, admin_user, seeded):
        groups = (await _get(client, admin_user, "summary", _q(seeded, group_by="day")))["groups"]
        assert [g["key"]["day"] for g in groups] == sorted(g["key"]["day"] for g in groups)
        assert all(g["key"]["day"].endswith("T00:00:00+00:00") for g in groups)
        assert sum(g["calls"] for g in groups) == 4

    async def test_filters(self, client, admin_user, seeded):
        totals = lambda body: (body["totals"]["calls"], body["totals"]["cost_usd"])  # noqa: E731
        assert totals(await _get(client, admin_user, "summary", _q(seeded, model="free-y"))) == (1, 0.0)
        assert totals(await _get(client, admin_user, "summary", _q(seeded, provider="q"))) == (1, 1.0)
        assert totals(await _get(client, admin_user, "summary", _q(seeded, source="provider_batch"))) == (1, 1.0)
        assert totals(await _get(client, admin_user, "summary", _q(seeded, user_id=str(seeded["other"].id)))) == (1, 1.0)
        since = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        assert totals(await _get(client, admin_user, "summary", _q(seeded, **{"from": since}))) == (5, 101.75)

    async def test_chat_ids_need_no_window(self, client, admin_user, seeded):
        """The cost of a set of chats, however old: what a per-run cost cap reads."""
        body = await _get(client, admin_user, "summary", {"chat_id": [str(seeded["chat_a"]), str(seeded["chat_b"])]})
        assert body["from"] is None
        assert (body["totals"]["calls"], body["totals"]["cost_usd"], body["totals"]["unpriced_calls"]) == (4, 100.75, 1)

    async def test_bad_requests(self, client, admin_user):
        h = auth_headers(admin_user)
        assert (await client.get("/api/v1/usage/summary?group_by=planet", headers=h)).status_code == 400
        assert (await client.get("/api/v1/usage/summary?group_by=day,hour", headers=h)).status_code == 400
        r = await client.get("/api/v1/usage/summary?from=2026-02-01T00:00:00&to=2026-01-01T00:00:00", headers=h)
        assert r.status_code == 400
        assert (await client.get("/api/v1/usage/summary?chat_id=nope", headers=h)).status_code == 422


class TestPermissions:
    async def test_read_own_sees_only_its_own_calls_whatever_the_filters(self, client, admin_user, seeded):
        other = seeded["other"]
        body = await _get(client, other, "summary", _q(seeded))
        assert (body["totals"]["calls"], body["totals"]["cost_usd"]) == (1, 1.0)
        body = await _get(client, other, "summary", _q(seeded, user_id=str(admin_user.id), group_by="user"))
        assert body["totals"]["calls"] == 0 and body["groups"] == []
        body = await _get(client, other, "summary", {"chat_id": str(seeded["chat_a"])})
        assert body["totals"]["calls"] == 0
        calls = await _get(client, other, "calls", _q(seeded))
        assert {c["user_id"] for c in calls["items"]} == {str(other.id)}
        points = (await _get(client, other, "timeseries", _q(seeded)))["points"]
        assert sum(p["calls"] for p in points) == 1

    async def test_without_a_usage_permission(self, client, db):
        nobody = await _user_with(db, "sinas.logs.read:own")
        h = auth_headers(nobody)
        for path in ("summary", "timeseries", "calls", "ratecard"):
            assert (await client.get(f"/api/v1/usage/{path}", headers=h)).status_code == 403, path
        own = await _user_with(db, "sinas.usage.read:own")
        assert (await client.get("/api/v1/usage/ratecard", headers=auth_headers(own))).status_code == 403
        assert (await client.post("/api/v1/usage/backfill", headers=auth_headers(own))).status_code == 403

    def test_users_get_read_own_by_default(self):
        from app.core.permissions import DEFAULT_ROLE_PERMISSIONS

        assert DEFAULT_ROLE_PERMISSIONS["Users"]["sinas.usage.read:own"] is True
        assert "sinas.usage.read:all" not in DEFAULT_ROLE_PERMISSIONS["Users"]


class TestTimeseriesAndCalls:
    async def test_hourly_buckets_fill_the_gaps(self, client, admin_user, seeded):
        now = datetime.now(timezone.utc)
        params = _q(seeded, interval="hour", **{"from": (now - timedelta(hours=30)).isoformat(), "to": now.isoformat()})
        body = await _get(client, admin_user, "timeseries", params)
        points = body["points"]
        assert len(points) in (30, 31)
        assert sum(p["calls"] for p in points) == 4
        assert sum(p["cost_usd"] for p in points) == pytest.approx(1.75)
        assert [p["bucket"] for p in points] == sorted(p["bucket"] for p in points)

    async def test_daily_by_default_and_bounded(self, client, admin_user, seeded):
        body = await _get(client, admin_user, "timeseries", _q(seeded))
        assert body["interval"] == "day" and len(body["points"]) in (30, 31)
        h = auth_headers(admin_user)
        r = await client.get("/api/v1/usage/timeseries?interval=hour&from=2020-01-01T00:00:00Z", headers=h)
        assert r.status_code == 400
        assert (await client.get("/api/v1/usage/timeseries?interval=week", headers=h)).status_code == 400

    async def test_calls_page_newest_first(self, client, admin_user, seeded):
        first = await _get(client, admin_user, "calls", _q(seeded, limit=3))
        assert len(first["items"]) == 3 and first["has_more"] is True
        stamps = [c["created_at"] for c in first["items"]]
        assert stamps == sorted(stamps, reverse=True)
        rest = await _get(client, admin_user, "calls", _q(seeded, limit=3, offset=3))
        assert len(rest["items"]) == 1 and rest["has_more"] is False
        unpriced = await _get(client, admin_user, "calls", _q(seeded, unpriced_only="true"))
        assert [c["model"] for c in unpriced["items"]] == ["free-y"]
        batch = await _get(client, admin_user, "calls", _q(seeded, provider="q"))
        assert batch["items"][0]["batch"] is True and batch["items"][0]["price_source"] == "provider"


class TestRatecard:
    async def test_effective_prices_their_origin_and_unpriced_models(self, client, db, admin_user, monkeypatch):
        monkeypatch.setattr(settings, "model_prices_json", json.dumps({
            "gpt-4o": {"input_cost_per_token": 2.5e-06, "output_cost_per_token": 1e-05, "litellm_provider": "openai"},
        }))
        name = f"prov-{uuid.uuid4().hex[:8]}"
        db.add(LLMProvider(
            name=name, provider_type="openai",
            pricing={"own-model": {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06}},
            model_aliases={"prod": "gpt-4o"}, pricing_discount=0.9,
        ))
        unknown = f"mystery-{uuid.uuid4().hex[:6]}"
        db.add_all([
            _row(provider_name=name, model="prod"), _row(provider_name=name, model="own-model"),
            _row(provider_name=name, model=unknown), _row(provider_name=name, model=unknown),
        ])
        await db.flush()

        body = await _get(client, admin_user, "ratecard", {"search": "gpt"})
        json_source = next(s for s in body["sources"] if s["source"] == "json")
        assert json_source["configured"] and json_source["models"] == 1
        provider = next(p for p in body["providers"] if p["name"] == name)
        assert provider["model_aliases"] == {"prod": "gpt-4o"} and provider["discount"] == 0.9
        assert provider["pricing"]["own-model"] == {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06}
        mine = {m["model"]: m for m in body["models_in_use"] if m["provider"] == name}
        assert (mine["prod"]["source"], mine["prod"]["matched_key"], mine["prod"]["origin"], mine["prod"]["discount"]) == (
            "deployment", "gpt-4o", "json", 0.9,
        )
        assert (mine["own-model"]["source"], mine["own-model"]["origin"]) == ("provider", f"provider:{name}")
        assert mine[unknown]["source"] is None
        unpriced = [m for m in body["unpriced_models"] if m["provider"] == name]
        assert [(m["model"], m["calls"]) for m in unpriced] == [(unknown, 2)]
        assert [e["model"] for e in body["deployment_entries"]] == ["gpt-4o"]

    async def test_refresh_needs_a_url(self, client, admin_user):
        r = await client.post("/api/v1/usage/ratecard/refresh", headers=auth_headers(admin_user))
        assert r.status_code == 400


class TestBackfillEndpoint:
    async def test_backfill_prices_the_range(self, client, db, admin_user, monkeypatch):
        model = f"bf-{uuid.uuid4().hex[:6]}"
        monkeypatch.setattr(settings, "model_prices_json", json.dumps(
            {model: {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06}}
        ))
        row = _row(model=model, provider_name=f"gone-{uuid.uuid4().hex[:6]}")
        db.add(row)
        await db.flush()
        now = datetime.now(timezone.utc)
        r = await client.post(
            "/api/v1/usage/backfill", headers=auth_headers(admin_user),
            json={"from": (now - timedelta(days=1)).isoformat(), "to": now.isoformat()},
        )
        assert r.status_code == 200, r.text
        assert r.json()["priced_calls"] >= 1
        await db.refresh(row)
        assert (row.cost_usd, row.price_source) == (Decimal("0.00011"), "backfill")


async def test_message_stats_include_cost(client, db, admin_user):
    before = (await client.get("/api/v1/messages/stats", headers=auth_headers(admin_user))).json()
    db.add_all([_row(cost_usd=Decimal("0.125")), _row()])
    await db.flush()
    after = (await client.get("/api/v1/messages/stats", headers=auth_headers(admin_user))).json()
    assert after["llm_cost_usd"] - before["llm_cost_usd"] == pytest.approx(0.125)
    assert after["llm_unpriced_calls"] - before["llm_unpriced_calls"] == 1


async def test_usage_indexes_exist(db):
    """The migration and the model declare the same indexes."""
    rows = dict((await db.execute(text(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'llm_usage'"
    ))).all())
    assert "(agent, created_at)" in rows["idx_llm_usage_agent_created"]
    assert "(provider_name, created_at)" in rows["idx_llm_usage_provider_created"]
    assert "(user_id, created_at)" in rows["idx_llm_usage_user_created"]
