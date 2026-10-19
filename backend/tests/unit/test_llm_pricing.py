"""LLM pricing (app/services/llm_pricing.py, docs/design/usage-cost.md).

The ratecard is LiteLLM's model_prices_and_context_window.json format; a
call is priced from the provider's own pricing first, then the deployment
ratecard (inline JSON over file over URL); the cost is frozen when the row
is written, and only unpriced rows are ever backfilled.
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, text

from app.core.config import settings
from app.models import LLMUsage
from app.models.llm_provider import LLMProvider
from app.models.ratecard_cache import RatecardCache
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.openai_provider import OpenAIProvider
from app.services import llm_pricing
from app.services.llm_pricing import (
    ModelPrice,
    ProviderPricing,
    RatecardError,
    call_cost,
    match,
    parse_ratecard,
    price_usage,
    resolve,
)

# Shaped like LiteLLM's file (prices per token, as there).
LITELLM = {
    "sample_spec": {"input_cost_per_token": 0.0, "output_cost_per_token": 0.0, "litellm_provider": "x"},
    "gpt-4o": {
        "input_cost_per_token": 2.5e-06, "output_cost_per_token": 1e-05,
        "cache_read_input_token_cost": 1.25e-06,
        "input_cost_per_token_batches": 1.25e-06, "output_cost_per_token_batches": 5e-06,
        "litellm_provider": "openai", "max_tokens": 16384, "supports_vision": True,
    },
    "gpt-4o-2024-08-06": {
        "input_cost_per_token": 2.5e-06, "output_cost_per_token": 1e-05, "litellm_provider": "openai",
    },
    "azure/gpt-4o": {
        "input_cost_per_token": 2.75e-06, "output_cost_per_token": 1.1e-05, "litellm_provider": "azure",
    },
    "claude-sonnet-4-5": {
        "input_cost_per_token": 3e-06, "output_cost_per_token": 1.5e-05,
        "cache_read_input_token_cost": 3e-07, "cache_creation_input_token_cost": 3.75e-06,
        "input_cost_per_token_batches": 1.5e-06, "output_cost_per_token_batches": 7.5e-06,
        "litellm_provider": "anthropic",
    },
    "gemini/gemini-2.0-flash": {
        "input_cost_per_token": 1e-07, "output_cost_per_token": 4e-07, "litellm_provider": "gemini",
    },
    "dall-e-3": {"output_cost_per_image": 0.04, "litellm_provider": "openai"},
    "broken": {"input_cost_per_token": "abc", "output_cost_per_token": -1},
}


@pytest.fixture(autouse=True)
def _clean_ratecard(monkeypatch):
    """No deployment ratecard unless a test sets one; nothing cached across tests."""
    for name in ("model_prices_json", "model_prices_file", "model_prices_url"):
        monkeypatch.setattr(settings, name, None)
    llm_pricing.reset_cache()
    yield
    llm_pricing.reset_cache()


def _card() -> dict[str, ModelPrice]:
    return parse_ratecard(LITELLM, "json")


def _name() -> str:
    return f"prov-{uuid.uuid4().hex[:8]}"


class TestParsing:
    def test_litellm_format_keeps_priced_models_and_ignores_the_rest(self):
        card = _card()
        assert set(card) == {"gpt-4o", "gpt-4o-2024-08-06", "azure/gpt-4o", "claude-sonnet-4-5", "gemini/gemini-2.0-flash"}
        gpt = card["gpt-4o"]
        assert (gpt.input, gpt.output, gpt.cache_read, gpt.cache_write) == (
            Decimal("0.0000025"), Decimal("0.00001"), Decimal("0.00000125"), None,
        )
        assert (gpt.input_batch, gpt.output_batch, gpt.provider, gpt.origin) == (
            Decimal("0.00000125"), Decimal("0.000005"), "openai", "json",
        )
        # The entry is stored reduced to the fields pricing uses.
        assert set(gpt.to_entry()) == {
            "input_cost_per_token", "output_cost_per_token", "cache_read_input_token_cost",
            "input_cost_per_token_batches", "output_cost_per_token_batches", "litellm_provider",
        }

    def test_text_and_errors(self):
        assert parse_ratecard(json.dumps({"M": {"input_cost_per_token": 1e-6}}), "json")["m"].input == Decimal("0.000001")
        for bad in ("{not json", "[1, 2]"):
            with pytest.raises(RatecardError):
                parse_ratecard(bad, "json")


class TestMatching:
    def test_provider_qualified_key_before_the_bare_name(self):
        card = _card()
        assert match(card, "gpt-4o", "azure")[0] == "azure/gpt-4o"
        assert match(card, "gpt-4o", "azure_openai")[0] == "azure/gpt-4o"
        assert match(card, "gpt-4o", "openai")[0] == "gpt-4o"
        assert match(card, "gemini-2.0-flash", "gemini")[0] == "gemini/gemini-2.0-flash"
        assert match(card, "GPT-4o", "openai")[0] == "gpt-4o"
        assert match(card, "gpt-5", "openai") is None

    def test_dates_are_dropped_only_without_an_exact_match(self):
        card = _card()
        assert match(card, "gpt-4o-2024-08-06", "openai")[0] == "gpt-4o-2024-08-06"
        assert match(card, "gpt-4o-2024-11-20", "openai")[0] == "gpt-4o"
        assert match(card, "claude-sonnet-4-5-20250929", "anthropic")[0] == "claude-sonnet-4-5"
        assert match(card, "claude-sonnet-4-5@20250929", "anthropic")[0] == "claude-sonnet-4-5"
        assert match(card, "gpt-4o-mini", "openai") is None  # no prefix matching

    def test_provider_pricing_wins_and_aliases_rename_first(self):
        deployment = _card()
        provider = ProviderPricing.from_values(
            "az", "azure",
            {"my-deploy": {"input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06}},
            {"Prod-4o": "gpt-4o", "mini": "my-deploy"},
            0.8,
        )
        own = resolve("mini", "azure", provider, deployment)
        assert (own.source, own.key, own.multiplier, own.price.origin) == ("provider", "my-deploy", 1, "provider:az")
        aliased = resolve("prod-4o", "azure", provider, deployment)
        assert (aliased.source, aliased.key, aliased.priced_as) == ("deployment", "azure/gpt-4o", "gpt-4o")
        # The discount is for list prices only.
        assert aliased.multiplier == Decimal("0.8")
        assert resolve("unknown-deploy", "azure", provider, deployment) is None
        assert resolve(None, "azure", provider, deployment) is None
        # No provider row (deleted): the deployment ratecard alone.
        assert resolve("gpt-4o", "openai", None, deployment).key == "gpt-4o"


class TestCost:
    def test_anthropic_usage_charges_cache_tokens_apart(self):
        usage = AnthropicProvider(api_key="k").extract_usage(SimpleNamespace(usage=SimpleNamespace(
            input_tokens=100, output_tokens=50, cache_read_input_tokens=1000, cache_creation_input_tokens=200,
        )))
        assert usage["prompt_tokens"] == 1300
        cost = call_cost(
            _card()["claude-sonnet-4-5"],
            prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"],
            cache_read_tokens=usage["cache_read_tokens"], cache_write_tokens=usage["cache_write_tokens"],
        )
        # 100 uncached at 3e-6, 1000 read at 3e-7, 200 written at 3.75e-6, 50 out at 1.5e-5
        assert cost == Decimal("0.0003") + Decimal("0.0003") + Decimal("0.00075") + Decimal("0.00075")

    def test_openai_usage_cached_tokens_are_part_of_prompt_tokens(self):
        usage = OpenAIProvider(api_key="k").extract_usage(SimpleNamespace(usage=SimpleNamespace(
            prompt_tokens=1000, completion_tokens=100, total_tokens=1100,
            prompt_tokens_details=SimpleNamespace(cached_tokens=600),
        )))
        cost = call_cost(
            _card()["gpt-4o"],
            prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"],
            cache_read_tokens=usage["cache_read_tokens"], cache_write_tokens=usage["cache_write_tokens"],
        )
        # 400 uncached at 2.5e-6, 600 cached at 1.25e-6, 100 out at 1e-5
        assert cost == Decimal("0.001") + Decimal("0.00075") + Decimal("0.001")

    def test_missing_cache_price_falls_back_to_input_and_missing_price_is_unpriced(self):
        price = ModelPrice(input=Decimal("0.000002"), output=None)
        assert call_cost(price, prompt_tokens=10, completion_tokens=0, cache_read_tokens=4) == Decimal("0.00002")
        assert call_cost(price, prompt_tokens=10, completion_tokens=1) is None
        assert call_cost(price, prompt_tokens=0, completion_tokens=0) == Decimal(0)

    def test_batch_prices_and_the_discount_on_cache_tokens(self):
        sonnet = _card()["claude-sonnet-4-5"]
        regular = call_cost(sonnet, prompt_tokens=1200, completion_tokens=100, cache_read_tokens=1000)
        batched = call_cost(sonnet, prompt_tokens=1200, completion_tokens=100, cache_read_tokens=1000, batch=True)
        assert batched * 2 == regular
        # No batch prices listed: the regular ones (an upper bound).
        dated = _card()["gpt-4o-2024-08-06"]
        assert call_cost(dated, prompt_tokens=10, completion_tokens=10, batch=True) == call_cost(
            dated, prompt_tokens=10, completion_tokens=10
        )

    def test_multiplier_and_rounding(self):
        price = ModelPrice(input=Decimal("0.000000123"), output=Decimal(0))
        assert call_cost(price, prompt_tokens=1, completion_tokens=0, multiplier=Decimal("0.5")) == Decimal("0.0000000615")


class TestDeploymentRatecard:
    async def test_inline_json_over_file_over_url(self, db, monkeypatch, tmp_path):
        url = "https://prices.example/model_prices.json"
        db.add(RatecardCache(
            source="url", url=url, model_count=2, fetched_at=datetime.now(timezone.utc),
            entries={"gpt-4o": {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06},
                     "only-url": {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06}},
        ))
        await db.flush()
        path = tmp_path / "prices.json"
        path.write_text(json.dumps({"gpt-4o": {"input_cost_per_token": 2e-06, "output_cost_per_token": 2e-06},
                                    "only-file": {"input_cost_per_token": 2e-06}}))
        monkeypatch.setattr(settings, "model_prices_url", url)
        monkeypatch.setattr(settings, "model_prices_file", str(path))
        card = await llm_pricing.deployment_ratecard(db, fresh=True)
        assert (card["gpt-4o"].origin, card["only-url"].origin, card["only-file"].origin) == ("file", "url", "file")

        monkeypatch.setattr(settings, "model_prices_json", json.dumps({"gpt-4o": {"input_cost_per_token": 3e-06}}))
        card = await llm_pricing.deployment_ratecard(db, fresh=True)
        assert (card["gpt-4o"].origin, card["gpt-4o"].input) == ("json", Decimal("0.000003"))

        # A copy fetched from another URL than the one configured is not used.
        monkeypatch.setattr(settings, "model_prices_url", "https://elsewhere.example/p.json")
        card = await llm_pricing.deployment_ratecard(db, fresh=True)
        assert "only-url" not in card

    async def test_a_broken_source_is_skipped_and_reported(self, db, monkeypatch, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{nope")
        monkeypatch.setattr(settings, "model_prices_file", str(bad))
        monkeypatch.setattr(settings, "model_prices_json", json.dumps(LITELLM))
        card = await llm_pricing.deployment_ratecard(db, fresh=True)
        assert "gpt-4o" in card
        assert "MODEL_PRICES_FILE" in llm_pricing.deployment_layers()["file"].error

    async def test_cached_for_a_minute(self, db, monkeypatch):
        monkeypatch.setattr(settings, "model_prices_json", json.dumps(LITELLM))
        assert "gpt-4o" in await llm_pricing.deployment_ratecard(db)
        monkeypatch.setattr(settings, "model_prices_json", None)
        assert "gpt-4o" in await llm_pricing.deployment_ratecard(db)
        assert "gpt-4o" not in await llm_pricing.deployment_ratecard(db, fresh=True)


class TestUrlRefresh:
    URL = "https://prices.example/model_prices_and_context_window.json"

    async def test_fetch_is_hardened_and_failures_keep_the_last_good_copy(self, db, monkeypatch):
        from app.services import package_repositories

        monkeypatch.setattr(settings, "model_prices_url", self.URL)
        calls = []
        payload = {"body": json.dumps(LITELLM).encode()}

        async def fake_get(url, headers, **kwargs):
            calls.append((url, kwargs))
            if payload["body"] is None:
                raise package_repositories.RepositoryError("https://prices.example/x answered 503")
            return payload["body"]

        monkeypatch.setattr(package_repositories, "_get", fake_get)
        assert await llm_pricing.refresh_url_ratecard(db) is True
        assert calls[0][1] == {
            "max_bytes": settings.model_prices_max_bytes,
            "timeout": settings.model_prices_timeout_seconds,
            "allow_http": False,
        }
        row = await db.get(RatecardCache, "url")
        assert (row.url, row.model_count, row.last_error) == (self.URL, 5, None)
        good_fetch = row.fetched_at

        # Fresh: not fetched again unless forced.
        assert await llm_pricing.refresh_url_ratecard(db) is False
        assert len(calls) == 1

        payload["body"] = None  # the source is down
        assert await llm_pricing.refresh_url_ratecard(db, force=True) is False
        row = await db.get(RatecardCache, "url")
        assert "503" in row.last_error and row.model_count == 5 and row.fetched_at == good_fetch
        card = await llm_pricing.deployment_ratecard(db, fresh=True)
        assert card["gpt-4o"].origin == "url"

        payload["body"] = b'{"gpt-4o": {"max_tokens": 1}}'  # parses, prices nothing
        assert await llm_pricing.refresh_url_ratecard(db, force=True) is False
        assert "prices no model" in (await db.get(RatecardCache, "url")).last_error
        assert (await db.get(RatecardCache, "url")).model_count == 5

    async def test_due_after_the_refresh_interval_or_a_new_url(self, db, monkeypatch):
        from app.services import package_repositories

        monkeypatch.setattr(settings, "model_prices_url", self.URL)
        fetched = []

        async def fake_get(url, headers, **kwargs):
            fetched.append(url)
            return json.dumps(LITELLM).encode()

        monkeypatch.setattr(package_repositories, "_get", fake_get)
        db.add(RatecardCache(
            source="url", url=self.URL, model_count=1, entries={},
            fetched_at=datetime.now(timezone.utc) - timedelta(hours=settings.model_prices_refresh_hours + 1),
        ))
        await db.flush()
        assert await llm_pricing.refresh_url_ratecard(db) is True
        monkeypatch.setattr(settings, "model_prices_url", "https://prices.example/other.json")
        assert await llm_pricing.refresh_url_ratecard(db) is True
        assert fetched == [self.URL, "https://prices.example/other.json"]

    async def test_plain_http_is_refused(self, db, monkeypatch):
        monkeypatch.setattr(settings, "model_prices_url", "http://prices.example/p.json")
        assert await llm_pricing.refresh_url_ratecard(db, force=True) is False
        assert "non-https" in (await db.get(RatecardCache, "url")).last_error


async def _provider(db, **pricing) -> LLMProvider:
    row = LLMProvider(name=_name(), provider_type=pricing.pop("provider_type", "openai"), **pricing)
    db.add(row)
    await db.flush()
    return row


def _values(provider: LLMProvider, model: str, **extra) -> dict:
    return {
        "provider_name": provider.name, "provider_type": provider.provider_type, "model": model,
        "prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
        "cache_read_tokens": 0, "cache_write_tokens": 0, "streamed": False, **extra,
    }


class TestPricingRows:
    async def test_cost_is_frozen_when_the_row_is_written(self, db, monkeypatch):
        """_insert_usage prices the row on its own session; changing the price
        afterwards leaves the recorded cost alone."""
        from app.providers import tracking

        provider = await _provider(db, pricing={"m-1": {"input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06}})

        class _Session:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr("app.core.database.AsyncSessionLocal", lambda: _Session())
        chat_id = uuid.uuid4()
        await tracking._insert_usage(_values(provider, "m-1", chat_id=chat_id))
        row = (await db.execute(select(LLMUsage).where(LLMUsage.chat_id == chat_id))).scalar_one()
        assert (row.cost_usd, row.price_source, row.batch) == (Decimal("0.0012"), "provider", False)

        provider.pricing = {"m-1": {"input_cost_per_token": 1.0, "output_cost_per_token": 1.0}}
        await db.flush()
        await db.refresh(row)
        assert row.cost_usd == Decimal("0.0012")

    async def test_deployment_price_with_the_provider_discount(self, db, monkeypatch):
        monkeypatch.setattr(settings, "model_prices_json", json.dumps(LITELLM))
        provider = await _provider(db, provider_type="azure", model_aliases={"prod": "gpt-4o"}, pricing_discount=0.5)
        values = _values(provider, "prod")
        await price_usage(db, values)
        # azure/gpt-4o: 1000 * 2.75e-6 + 100 * 1.1e-5 = 0.00385, halved
        assert (values["cost_usd"], values["price_source"]) == (Decimal("0.001925"), "deployment")

    async def test_unpriced_and_failures_leave_the_cost_unset(self, db, monkeypatch):
        provider = await _provider(db)
        values = _values(provider, "nobody-priced-this")
        await price_usage(db, values)
        assert "cost_usd" not in values and "price_source" not in values

        async def broken(*args, **kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(llm_pricing, "provider_pricing", broken)
        values = _values(provider, "m")
        await price_usage(db, values)  # never raises
        assert "cost_usd" not in values

    async def test_batch_rows_are_priced_at_batch_rates(self, db, monkeypatch):
        monkeypatch.setattr(settings, "model_prices_json", json.dumps(LITELLM))
        provider = await _provider(db, provider_type="anthropic")
        regular, batch = _values(provider, "claude-sonnet-4-5"), _values(provider, "claude-sonnet-4-5", batch=True)
        await price_usage(db, regular)
        await price_usage(db, batch)
        assert batch["cost_usd"] * 2 == regular["cost_usd"]


class TestBackfill:
    async def test_prices_only_unpriced_rows_in_the_range(self, db, monkeypatch):
        provider = await _provider(db, provider_type="anthropic")
        model = f"m-{uuid.uuid4().hex[:6]}"
        now = datetime.now(timezone.utc)
        rows = {
            "old": LLMUsage(created_at=now - timedelta(days=10), **_values(provider, model)),
            "in": LLMUsage(created_at=now - timedelta(days=1), **_values(provider, model, cache_read_tokens=400, cache_write_tokens=100)),
            "batch": LLMUsage(created_at=now - timedelta(days=1), batch=True, **_values(provider, model)),
            "priced": LLMUsage(created_at=now - timedelta(days=1), cost_usd=Decimal("9"), price_source="provider", **_values(provider, model)),
            "no-output-price": LLMUsage(created_at=now - timedelta(days=1), **_values(provider, f"{model}-in")),
            "zero-output": LLMUsage(created_at=now - timedelta(days=1), **_values(provider, f"{model}-in", completion_tokens=0)),
        }
        db.add_all(rows.values())
        await db.flush()
        provider.pricing = {
            model: {
                "input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06,
                "cache_read_input_token_cost": 1e-07, "cache_creation_input_token_cost": 1.25e-06,
                "input_cost_per_token_batches": 5e-07, "output_cost_per_token_batches": 1e-06,
            },
            f"{model}-in": {"input_cost_per_token": 1e-06},
        }
        await db.flush()

        result = await llm_pricing.backfill(db, start=now - timedelta(days=2), end=now)
        assert result["priced_calls"] >= 3
        for row in rows.values():
            await db.refresh(row)

        assert rows["old"].cost_usd is None
        assert rows["priced"].cost_usd == Decimal("9") and rows["priced"].price_source == "provider"
        assert rows["no-output-price"].cost_usd is None
        assert rows["zero-output"].cost_usd == Decimal("0.001")
        # The SQL arithmetic agrees with call_cost, batch and cache included.
        price = llm_pricing.parse_entry(provider.pricing[model], "x")
        for key, batch in (("in", False), ("batch", True)):
            row = rows[key]
            assert row.price_source == "backfill"
            assert row.cost_usd == call_cost(
                price, prompt_tokens=row.prompt_tokens, completion_tokens=row.completion_tokens,
                cache_read_tokens=row.cache_read_tokens, cache_write_tokens=row.cache_write_tokens, batch=batch,
            )


async def test_provider_batch_results_are_recorded_as_batch_and_priced_so(db, admin_user, monkeypatch):
    """The batch poller writes one llm_usage row per result: marked batch,
    priced at the ratecard's batch rates."""
    from app.models.agent import Agent
    from app.models.batch import Batch, BatchKind, BatchStatus
    from app.models.chat import Chat
    from app.models.execution import Execution, ExecutionStatus, TriggerType
    from app.scheduler.jobs import poll_provider_batches

    monkeypatch.setattr(settings, "model_prices_json", json.dumps(LITELLM))
    provider = await _provider(db, provider_type="anthropic")
    ns = f"ns{uuid.uuid4().hex[:6]}"
    db.add(Agent(user_id=admin_user.id, namespace=ns, name="b", system_prompt="s", model="claude-sonnet-4-5"))
    chat = Chat(user_id=admin_user.id, agent_namespace=ns, agent_name="b", title="t")
    db.add(chat)
    now = datetime.now(timezone.utc)
    batch = Batch(
        user_id=admin_user.id, kind=BatchKind.AGENT, target_namespace=ns, target_name="b", total=1,
        status=BatchStatus.RUNNING, started_at=now, created_at=now, execution_mode="provider",
        provider_batch_id="pb-1", llm_provider_id=provider.id,
    )
    db.add(batch)
    await db.flush()
    execution_id = f"ex-{uuid.uuid4().hex[:8]}"
    db.add(Execution(
        user_id=admin_user.id, execution_id=execution_id, function_name=f"{ns}/b", trigger_type=TriggerType.API,
        trigger_id=str(batch.id), chat_id=chat.id, status=ExecutionStatus.RUNNING, input_data={},
        started_at=now, batch_id=batch.id,
    ))
    await db.flush()

    class _Provider:
        async def get_batch_status(self, provider_batch_id):
            return {"ended": True}

        async def fetch_batch_results(self, provider_batch_id):
            usage = {"prompt_tokens": 1200, "completion_tokens": 100, "total_tokens": 1300,
                     "cache_read_tokens": 1000, "cache_write_tokens": 0}
            return [{"custom_id": execution_id, "status": "succeeded", "content": "done", "usage": usage}]

    async def fake_create_provider(**kwargs):
        return _Provider()

    async def noop(**kwargs):
        return None

    class _Session:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(poll_provider_batches, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr("app.providers.create_provider", fake_create_provider)
    monkeypatch.setattr("app.services.batch_service.on_execution_terminated", noop)
    await poll_provider_batches._poll_one(batch.id)

    row = (await db.execute(select(LLMUsage).where(LLMUsage.chat_id == chat.id))).scalar_one()
    assert (row.batch, row.source, row.model, row.price_source) == (True, "provider_batch", "claude-sonnet-4-5", "deployment")
    regular = call_cost(_card()["claude-sonnet-4-5"], prompt_tokens=1200, completion_tokens=100, cache_read_tokens=1000)
    assert row.cost_usd * 2 == regular


class TestReviewFixes:
    async def test_a_failing_pricing_query_never_loses_the_write(self, db, monkeypatch):
        """A pricing read that errors aborts only its savepoint: the usage row
        and anything else the caller has pending (the batch poller's messages)
        still commit, unpriced."""
        from app.providers import tracking

        async def failing(session, name, **kwargs):
            await session.execute(text("SELECT 1/0"))  # aborts the transaction, as a timeout would

        monkeypatch.setattr(llm_pricing, "provider_pricing", failing)

        class _Session:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr("app.core.database.AsyncSessionLocal", lambda: _Session())
        provider = await _provider(db)
        chat_id = uuid.uuid4()
        await tracking._insert_usage(_values(provider, "m-1", chat_id=chat_id))
        row = (await db.execute(select(LLMUsage).where(LLMUsage.chat_id == chat_id))).scalar_one()
        assert (row.cost_usd, row.price_source) == (None, None)

        # The batch poller's shape: other writes pending in the same session.
        other = LLMProvider(name=_name(), provider_type="openai")
        db.add(other)
        values = _values(provider, "m-1")
        await price_usage(db, values)
        db.add(LLMUsage(**values))
        await db.commit()
        assert (await db.execute(select(LLMProvider.id).where(LLMProvider.name == other.name))).scalar_one()

    async def test_cached_pricing_makes_no_query(self, db, monkeypatch):
        monkeypatch.setattr(settings, "model_prices_json", json.dumps(LITELLM))
        provider = await _provider(db, provider_type="anthropic")
        await price_usage(db, _values(provider, "claude-sonnet-4-5"))  # warms both caches

        async def no_queries(*args, **kwargs):
            raise AssertionError("queried the database")

        monkeypatch.setattr(db, "execute", no_queries)
        monkeypatch.setattr(db, "begin_nested", no_queries)
        values = _values(provider, "claude-sonnet-4-5")
        await price_usage(db, values)
        assert values["price_source"] == "deployment"

    async def test_concurrent_reloads_never_see_a_half_loaded_url_card(self, db, monkeypatch):
        url = "https://prices.example/p.json"
        monkeypatch.setattr(settings, "model_prices_url", url)
        db.add(RatecardCache(
            source="url", url=url, model_count=1, fetched_at=datetime.now(timezone.utc),
            entries={"gpt-4o": {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06}},
        ))
        await db.flush()
        real_entries = llm_pricing._url_entries

        async def slow_entries(session):
            await asyncio.sleep(0.05)
            return await real_entries(session)

        monkeypatch.setattr(llm_pricing, "_url_entries", slow_entries)
        cards = await asyncio.gather(
            llm_pricing.deployment_ratecard(db, fresh=True), llm_pricing.deployment_ratecard(db, fresh=True)
        )
        assert all("gpt-4o" in card for card in cards)

    async def test_a_failed_url_load_publishes_nothing_and_stays_due(self, db, monkeypatch):
        url = "https://prices.example/p.json"
        monkeypatch.setattr(settings, "model_prices_url", url)
        db.add(RatecardCache(
            source="url", url=url, model_count=1, fetched_at=datetime.now(timezone.utc),
            entries={"gpt-4o": {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06}},
        ))
        await db.flush()
        real_entries = llm_pricing._url_entries
        calls = []

        async def flaky_entries(session):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("connection reset")
            return await real_entries(session)

        monkeypatch.setattr(llm_pricing, "_url_entries", flaky_entries)
        with pytest.raises(RuntimeError):
            await llm_pricing.deployment_ratecard(db)
        assert llm_pricing.deployment_layers()["url"].key is None
        # Not cached as loaded: the next call (no `fresh`) loads it.
        assert "gpt-4o" in await llm_pricing.deployment_ratecard(db)

    async def test_first_refreshes_at_once_dont_collide(self, monkeypatch):
        """Two sessions create the cache row at the same time (an admin's
        refresh and the cron's): both succeed."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.services import package_repositories
        from tests.conftest import _test_db_url

        monkeypatch.setattr(settings, "model_prices_url", "https://prices.example/race.json")

        # Both fetches finish together, so both refreshes reach the row
        # before either commits.
        started, both = [], asyncio.Event()

        async def fake_get(url, headers, **kwargs):
            started.append(url)
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), timeout=5)
            return json.dumps(LITELLM).encode()

        monkeypatch.setattr(package_repositories, "_get", fake_get)
        engine = create_async_engine(_test_db_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def clear():
            async with sessions() as s:
                await s.execute(delete(RatecardCache).where(RatecardCache.source == "url"))
                await s.commit()

        async def refresh():
            async with sessions() as s:
                stored = await llm_pricing.refresh_url_ratecard(s, force=True)
                await s.commit()
                return stored

        await clear()
        try:
            assert await asyncio.gather(refresh(), refresh()) == [True, True]
            async with sessions() as s:
                assert (await s.get(RatecardCache, "url")).model_count == 5
        finally:
            await clear()
            await engine.dispose()

    async def test_backfill_counts_hold_while_calls_keep_arriving(self, db, monkeypatch):
        provider = await _provider(db, pricing={"m-live": {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06}})
        db.add(LLMUsage(created_at=datetime.now(timezone.utc) - timedelta(minutes=5), **_values(provider, "m-live")))
        await db.flush()
        real = llm_pricing.provider_pricing
        late = LLMUsage(created_at=datetime.now(timezone.utc) + timedelta(seconds=1), **_values(provider, "m-live"))

        async def call_arrives(session, name, **kwargs):
            if late not in session:
                session.add(late)  # recorded while the backfill runs
                await session.flush()
            return await real(session, name, **kwargs)

        monkeypatch.setattr(llm_pricing, "provider_pricing", call_arrives)
        result = await llm_pricing.backfill(db, start=datetime.now(timezone.utc) - timedelta(hours=1))
        assert result["unpriced_calls"] >= 0
        assert result["examined_calls"] == result["priced_calls"] + result["unpriced_calls"]
        await db.refresh(late)
        assert late.cost_usd is None  # after the range's end: left alone


def test_concurrent_indexes_have_a_revision_of_their_own():
    """A failed CONCURRENTLY build must not strand the columns migration:
    it lives in a revision of its own, which drops leftovers first."""
    import importlib.util
    from pathlib import Path

    versions = Path(__file__).resolve().parents[2] / "alembic" / "versions"

    def load(name):
        spec = importlib.util.spec_from_file_location(name, versions / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    columns = load("c1o2s3t4u5s6_llm_usage_cost")
    indexes = load("c2u3s4i5d6x7_llm_usage_cost_indexes")
    assert indexes.down_revision == columns.revision
    assert "CONCURRENTLY" not in (versions / "c1o2s3t4u5s6_llm_usage_cost.py").read_text().split("def upgrade")[1]
    source = (versions / "c2u3s4i5d6x7_llm_usage_cost_indexes.py").read_text()
    for index in ("idx_llm_usage_agent_created", "idx_llm_usage_provider_created"):
        assert source.index(f"DROP INDEX CONCURRENTLY IF EXISTS {index}") < source.index(f"CREATE INDEX CONCURRENTLY {index}")


# OpenAI-compatible APIs of other providers, as LiteLLM lists them.
COMPATIBLE = {
    **LITELLM,
    "mistral/mistral-large-latest": {
        "input_cost_per_token": 2e-06, "output_cost_per_token": 6e-06, "litellm_provider": "mistral",
    },
    "mistral/gpt-4o": {  # a clash, to show the family's own key wins
        "input_cost_per_token": 9e-06, "output_cost_per_token": 9e-06, "litellm_provider": "mistral",
    },
    "groq/llama-3.1-8b-instant": {
        "input_cost_per_token": 5e-08, "output_cost_per_token": 8e-08, "litellm_provider": "groq",
    },
    "vertex_ai/claude-sonnet-4-5": {
        "input_cost_per_token": 3e-06, "output_cost_per_token": 1.5e-05,
        "litellm_provider": "vertex_ai-anthropic_models",
    },
    "gemini-1.5-pro": {
        "input_cost_per_token": 1.25e-06, "output_cost_per_token": 5e-06,
        "litellm_provider": "vertex_ai-language-models",
    },
    "low/1024-x-1024/some-image-model": {  # a size prefix, not a family
        "input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06, "litellm_provider": "openai",
    },
}


class TestPricingProvider:
    """pricing_provider: an openai-type provider pointed at another
    provider's OpenAI-compatible API is priced as that provider's family."""

    def test_the_family_replaces_the_provider_types_prefix(self):
        card = parse_ratecard(COMPATIBLE, "json")
        # The openai type alone never looks under mistral/.
        assert match(card, "mistral-large-latest", "openai") is None
        assert match(card, "mistral-large-latest", "openai", "mistral")[0] == "mistral/mistral-large-latest"
        assert match(card, "llama-3.1-8b-instant", "openai", "groq")[0] == "groq/llama-3.1-8b-instant"
        # In place of the type's prefixes, not besides them.
        assert match(card, "gpt-4o", "azure", "mistral")[0] == "mistral/gpt-4o"
        assert match(card, "gpt-4o", "azure")[0] == "azure/gpt-4o"

    def test_falls_back_to_the_bare_name(self):
        card = parse_ratecard(COMPATIBLE, "json")
        assert match(card, "claude-sonnet-4-5", "openai", "groq")[0] == "claude-sonnet-4-5"
        assert match(card, "claude-sonnet-4-5-20250929", "openai", "vertex_ai")[0] == "vertex_ai/claude-sonnet-4-5"
        assert match(card, "nobody-has-this", "openai", "groq") is None

    def test_an_alias_and_the_family_together(self):
        deployment = parse_ratecard(COMPATIBLE, "json")
        provider = ProviderPricing.from_values(
            "la-plateforme", "openai",
            {"own-model": {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06}},
            {"large": "mistral-large-latest"}, 0.5, "mistral",
        )
        hit = resolve("large", "openai", provider, deployment)
        assert (hit.source, hit.key, hit.priced_as, hit.multiplier) == (
            "deployment", "mistral/mistral-large-latest", "mistral-large-latest", Decimal("0.5"),
        )
        own = resolve("own-model", "openai", provider, deployment)
        assert (own.source, own.key) == ("provider", "own-model")

    def test_unset_prices_as_before(self):
        deployment = parse_ratecard(COMPATIBLE, "json")
        for unset in (None, "", "  "):
            provider = ProviderPricing.from_values("p", "openai", None, None, None, unset)
            assert provider.pricing_provider is None
            assert resolve("gpt-4o", "openai", provider, deployment).key == "gpt-4o"
            assert resolve("mistral-large-latest", "openai", provider, deployment) is None
        # The five-argument form older callers use.
        assert ProviderPricing.from_values("p", "openai", None, None, None).pricing_provider is None

    def test_provider_families(self):
        families = llm_pricing.provider_families(parse_ratecard(COMPATIBLE, "json"))
        assert families == {
            "anthropic": 1, "azure": 1, "gemini": 1, "groq": 1, "mistral": 2, "openai": 3,
            # A prefix naming its entry's provider family counts as one.
            "vertex_ai": 1, "vertex_ai-anthropic_models": 1, "vertex_ai-language-models": 1,
        }
        assert "low" not in families
        assert llm_pricing.provider_families({}) == {}

    async def test_new_calls_and_the_backfill_use_it(self, db, monkeypatch):
        monkeypatch.setattr(settings, "model_prices_json", json.dumps(COMPATIBLE))
        provider = await _provider(db, pricing_provider="mistral")
        values = _values(provider, "mistral-large-latest")
        await price_usage(db, values)
        # 1000 * 2e-6 + 100 * 6e-6
        assert (values["cost_usd"], values["price_source"]) == (Decimal("0.0026"), "deployment")

        # Recorded before pricing_provider was set: one unpriced, and one
        # already priced that the backfill must leave alone.
        later = await _provider(db)
        now = datetime.now(timezone.utc)
        unpriced = LLMUsage(created_at=now - timedelta(minutes=5), **_values(later, "mistral-large-latest"))
        priced = LLMUsage(
            created_at=now - timedelta(minutes=5), cost_usd=Decimal("1"), price_source="provider",
            **_values(later, "mistral-large-latest"),
        )
        db.add_all([unpriced, priced])
        await db.flush()
        later.pricing_provider = "mistral"
        await db.flush()
        await llm_pricing.backfill(db, start=now - timedelta(hours=1), end=now)
        await db.refresh(unpriced)
        await db.refresh(priced)
        assert (unpriced.cost_usd, unpriced.price_source) == (Decimal("0.0026"), "backfill")
        assert (priced.cost_usd, priced.price_source) == (Decimal("1"), "provider")
