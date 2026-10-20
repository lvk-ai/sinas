"""Budget and rate refusals: a typed error, surfaced everywhere, never retried.

Pinned here: the conservative classification (status 402, well-known error
types, gateway budget messages, a provider's own rules; rate_limited only for
a 429 that is no budget refusal); the SDKs don't retry a budget refusal; the
usage-tracking wrapper raises it typed and records it; an API call answers it
as 402 `budget_exceeded`; a streamed turn ends on an `error` event with the
code; a queued job's status and a pipeline step record the code.
"""
import asyncio
import json
import uuid

import anthropic
import httpx
import httpx2
import openai
import pytest

from app.providers import AnthropicProvider, OpenAIProvider
from app.providers.anthropic_provider import anthropic_client
from app.providers.errors import (
    BUDGET_EXCEEDED,
    RATE_LIMITED,
    LLMRefusalError,
    classify,
    refusal_code,
    refusal_from_exception,
    validate_budget_rules,
)
from app.providers.openai_provider import openai_client
from app.providers.tracking import UsageTrackingProvider

MESSAGES = [{"role": "user", "content": "hi"}]


# ── classification ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status, body",
    [
        (402, {"error": {"message": "Payment required"}}),
        (402, None),
        # OpenAI: quota exhausted.
        (429, {"error": {"message": "You exceeded your current quota", "type": "insufficient_quota", "code": "insufficient_quota"}}),
        # A gateway's per-key / per-user budget, as a 400 or a 429.
        (400, {"error": {"message": "Budget has been exceeded! Current cost: 1.2, Max budget: 1.0", "type": "budget_exceeded", "param": None, "code": "400"}}),
        (429, {"error": {"message": "User: u-1, exceeded budget for model=m", "type": "budget_exceeded", "code": "429"}}),
        # A gateway's per-customer budget, under an auth error type.
        (401, {"error": {"message": "ExceededBudget: End User=org-42 over budget. Spend=2.0, Budget=1.0", "type": "auth_error", "param": "None", "code": 401}}),
        (401, {"detail": "Authentication Error, ExceededTokenBudget: Current spend for token: 7.2; Max Budget for Token: 2"}),
        # A gateway's spend cap as a 403.
        (403, {"error": {"code": 403, "message": "Budget limit exceeded (weekly limit).", "metadata": {}}}),
        # Anthropic: billing.
        (400, {"type": "error", "error": {"type": "invalid_request_error", "message": "Your credit balance is too low to access the API."}}),
        (402, {"type": "error", "error": {"type": "billing_error", "message": "billing"}}),
    ],
)
def test_budget_refusals_are_recognised(status, body):
    assert classify(status, body) == BUDGET_EXCEEDED


@pytest.mark.parametrize(
    "status, body, expected",
    [
        (429, {"error": {"message": "Rate limit reached for requests", "type": "requests", "code": "rate_limit_exceeded"}}, RATE_LIMITED),
        (429, None, RATE_LIMITED),
        (400, {"error": {"message": "max_tokens is too large", "type": "invalid_request_error"}}, None),
        (401, {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}}, None),
        (403, {"error": {"message": "Forbidden"}}, None),
        (500, {"error": {"message": "Internal error"}}, None),
        (529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, None),
    ],
)
def test_other_errors_are_not_budget_refusals(status, body, expected):
    assert classify(status, body) == expected


def test_a_providers_own_rules_map_its_gateways_error():
    body = {"message": "Monthly spend cap reached for tenant"}
    assert classify(403, body) is None
    rules = validate_budget_rules([{"status": [403, 429], "message_contains": "spend cap"}])
    assert classify(403, body, rules=rules) == BUDGET_EXCEEDED
    assert classify(429, body, rules=rules) == BUDGET_EXCEEDED
    assert classify(500, body, rules=rules) is None
    by_type = validate_budget_rules([{"type": "tenant_quota"}])
    assert classify(400, {"error": {"type": "tenant_quota", "message": "x"}}, rules=by_type) == BUDGET_EXCEEDED


@pytest.mark.parametrize(
    "rules, problem",
    [
        ({"status": 402}, "list"),
        ([{}], "object"),
        ([{"status": 200}], "HTTP error status"),
        ([{"status": True}], "HTTP error status"),
        ([{"message": "x"}], "unknown key"),
        ([{"type": ""}], "non-empty"),
        ([{"type": "x"}] * 21, "at most"),
    ],
)
def test_invalid_rules_are_refused(rules, problem):
    with pytest.raises(ValueError, match=problem):
        validate_budget_rules(rules)


def test_the_provider_spec_checks_its_rules():
    from app.schemas.spec.llm_provider import LLMProviderSpec

    spec = LLMProviderSpec(name="gw", provider_type="openai", config={"budget_errors": [{"status": 403}]})
    assert spec.config["budget_errors"] == [{"status": 403}]
    with pytest.raises(ValueError):
        LLMProviderSpec(name="gw", provider_type="openai", config={"budget_errors": [{"status": 99}]})


def _status_error(cls, status, body):
    request = httpx.Request("POST", "http://gateway.test/v1/chat/completions")
    response = httpx.Response(status, request=request, json=body, headers={"retry-after": "7"})
    return cls(message=json.dumps(body), response=response, body=body)


def test_an_sdk_error_becomes_a_typed_refusal():
    error = _status_error(openai.RateLimitError, 429, {"error": {"type": "insufficient_quota", "message": "quota"}})
    refusal = refusal_from_exception(error, provider="gw")
    assert refusal.code == BUDGET_EXCEEDED and refusal.http_status == 402
    assert refusal.upstream_status == 429
    assert str(refusal).startswith("budget_exceeded: LLM provider 'gw' refused the call")

    limited = refusal_from_exception(_status_error(openai.RateLimitError, 429, {"error": {"message": "slow down"}}))
    assert limited.code == RATE_LIMITED and limited.http_status == 429 and limited.retry_after == "7"

    assert refusal_from_exception(_status_error(openai.BadRequestError, 400, {"error": {"message": "bad"}})) is None
    assert refusal_from_exception(ValueError("no status")) is None
    assert refusal_from_exception(refusal) is None  # already one


def test_refusal_codes_of_exceptions_and_events():
    assert refusal_code(LLMRefusalError(BUDGET_EXCEEDED, "x")) == BUDGET_EXCEEDED
    assert refusal_code({"type": "error", "code": "budget_exceeded"}) == BUDGET_EXCEEDED
    assert refusal_code({"type": "error", "error": "boom"}) is None
    # An SDK error's own `code` attribute is the provider's, not ours.
    raw = _status_error(openai.BadRequestError, 400, {"error": {"code": "budget_exceeded", "message": "x"}})
    assert refusal_code(raw) is None
    event = {"type": "error", "error": "budget_exceeded: out of budget", "code": "budget_exceeded"}
    again = LLMRefusalError.from_event(event)
    assert again.code == BUDGET_EXCEEDED and str(again) == "budget_exceeded: out of budget"


# ── never retried ──────────────────────────────────────────────────────────


class _Refusing:
    """Answers every request with the same error, counting them."""

    def __init__(self, status, body):
        self.status, self.body, self.calls = status, body, 0

    def handle(self, response_class):
        def handler(request):
            self.calls += 1
            return response_class(self.status, json=self.body)

        return handler


@pytest.fixture(autouse=False)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(openai._base_client.AsyncAPIClient, "_calculate_retry_timeout", lambda *a, **k: 0)
    monkeypatch.setattr(anthropic._base_client.AsyncAPIClient, "_calculate_retry_timeout", lambda *a, **k: 0)


def _openai_on(wire, max_retries=2):
    provider = OpenAIProvider(api_key="k", base_url="http://gateway.test/v1")
    provider.client = openai_client(
        api_key="k", base_url="http://gateway.test/v1", max_retries=max_retries,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(wire.handle(httpx.Response))),
    )
    return provider


def _anthropic_on(wire, max_retries=2):
    provider = AnthropicProvider(api_key="k", base_url="http://gateway.test")
    provider.client = anthropic_client(
        api_key="k", base_url="http://gateway.test", max_retries=max_retries,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(wire.handle(httpx2.Response))),
    )
    return provider


async def _call(provider, streamed):
    if streamed:
        async for _ in provider.stream(MESSAGES, model="m", max_tokens=1):
            pass
    else:
        await provider.complete(MESSAGES, model="m", max_tokens=1)


@pytest.mark.parametrize("streamed", [False, True], ids=["complete", "stream"])
@pytest.mark.parametrize("make", [_openai_on, _anthropic_on], ids=["openai", "anthropic"])
def test_the_sdk_does_not_retry_a_budget_refusal(make, streamed, _no_backoff):
    budget = _Refusing(429, {"error": {"type": "budget_exceeded", "message": "Budget has been exceeded!"}})
    provider = UsageTrackingProvider(make(budget), provider_name="gw", provider_type="openai")
    with pytest.raises(LLMRefusalError) as caught:
        asyncio.run(_call(provider, streamed))
    assert caught.value.code == BUDGET_EXCEEDED
    assert budget.calls == 1

    # An ordinary rate limit is still retried by the SDK (2 retries).
    limited = _Refusing(429, {"error": {"message": "Rate limit reached"}})
    provider = UsageTrackingProvider(make(limited), provider_name="gw", provider_type="openai")
    with pytest.raises(LLMRefusalError) as caught:
        asyncio.run(_call(provider, streamed))
    assert caught.value.code == RATE_LIMITED
    assert limited.calls == 3


def test_a_providers_rules_stop_the_retries_too(_no_backoff):
    cap = _Refusing(429, {"message": "tenant spend cap reached"})
    inner = _openai_on(cap)
    inner.budget_rules = [{"status": 429, "message_contains": "spend cap"}]
    provider = UsageTrackingProvider(inner, provider_name="gw", budget_rules=inner.budget_rules)
    with pytest.raises(LLMRefusalError) as caught:
        asyncio.run(_call(provider, False))
    assert caught.value.code == BUDGET_EXCEEDED and cap.calls == 1


def test_the_refusal_is_recorded_on_the_usage_row(monkeypatch):
    from app.providers import tracking

    rows = []

    async def insert(values):
        rows.append(values)
        return True

    monkeypatch.setattr(tracking, "_insert_usage", insert)
    budget = _Refusing(402, {"error": {"message": "out of credits"}})
    provider = UsageTrackingProvider(
        _openai_on(budget, max_retries=0), provider_name="gw", provider_type="openai",
        context={"user_id": str(uuid.uuid4()), "customer": "org-42"},
    )

    async def run():
        with pytest.raises(LLMRefusalError):
            await _call(provider, False)
        await asyncio.sleep(0)

    asyncio.run(run())
    (row,) = rows
    assert row["error"].startswith("budget_exceeded: ")
    assert row["customer"] == "org-42"


# ── surfaced ───────────────────────────────────────────────────────────────


async def test_the_api_answers_a_refusal_as_402_budget_exceeded():
    from httpx import ASGITransport, AsyncClient

    from app.main import adapters_openai_app, app, management_app

    for target in (app, management_app, adapters_openai_app):
        assert LLMRefusalError in target.exception_handlers

    @app.get("/__test_refusal")
    async def _refuse():
        raise LLMRefusalError(BUDGET_EXCEEDED, "LLM provider 'gw' refused the call (budget or quota exhausted, HTTP 400): x")

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/__test_refusal")
    finally:
        app.router.routes = [r for r in app.router.routes if getattr(r, "path", None) != "/__test_refusal"]
    assert resp.status_code == 402
    assert resp.json()["code"] == "budget_exceeded"
    assert resp.json()["detail"].startswith("budget_exceeded: ")


def test_a_follower_that_sees_a_refused_turn_raises_it_typed():
    from app.services import sync_turns

    follower = sync_turns.RoundFollower("ch", timeout=1)
    follower.status = "failed"
    follower.error_event = {"type": "error", "error": "budget_exceeded: out", "code": "budget_exceeded"}
    follower.error = follower.error_event["error"]

    async def run():
        async def fake_run(self):
            return follower

        sync_turns.RoundFollower.run, original = fake_run, sync_turns.RoundFollower.run
        try:
            await sync_turns.settle_turn(
                None, str(uuid.uuid4()), status=sync_turns.TURN_IN_PROGRESS, message=None,
                channel_id="ch", usage_parts=[], budget=1,
            )
        finally:
            sync_turns.RoundFollower.run = original

    with pytest.raises(LLMRefusalError) as caught:
        asyncio.run(run())
    assert caught.value.code == BUDGET_EXCEEDED


def test_a_failed_job_records_the_code():
    from app.queue.agent_jobs import _failure_fields

    assert _failure_fields(LLMRefusalError(BUDGET_EXCEEDED, "out")) == {
        "error": "budget_exceeded: out", "error_code": "budget_exceeded",
    }
    assert _failure_fields(RuntimeError("boom")) == {"error": "boom"}


def test_a_pipeline_step_keeps_the_code():
    from app.services.pipeline_runner import StepError

    assert StepError("x", code="budget_exceeded").code == "budget_exceeded"
    assert StepError("x").code is None


class _StatusRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None, **kw):
        self.values[key] = value


def test_a_queued_turn_ended_by_a_refusal_fails_its_job_with_the_code(monkeypatch):
    """The turn's own error handling streams the refusal as an `error` event
    with its code (and keeps the chat valid); the job then fails with it, so
    its status record (and a batch execution, a parent delegation) say
    budget_exceeded, and no second error event follows."""
    from app.queue import agent_jobs
    from app.services import message_service as ms
    from app.services.queue_service import JOB_STATUS_PREFIX
    from app.services.stream_relay import stream_relay

    class _Refused:
        def __init__(self, db):
            pass

        async def send_message_stream(self, **kwargs):
            yield {
                "content": "An error occurred ...", "type": "error",
                "error": "budget_exceeded: LLM provider 'gw' refused the call", "code": "budget_exceeded",
            }

    published = []

    async def publish(channel_id, event, ttl=None):
        published.append(event)

    async def publish_error(channel_id, error, code=None):
        published.append({"type": "error", "error": error, "code": code})

    async def publish_done(channel_id, **kw):
        published.append({"type": "done"})

    monkeypatch.setattr(ms, "MessageService", _Refused)
    monkeypatch.setattr(stream_relay, "publish", publish)
    monkeypatch.setattr(stream_relay, "publish_error", publish_error)
    monkeypatch.setattr(stream_relay, "publish_done", publish_done)
    redis = _StatusRedis()
    job_id = str(uuid.uuid4())

    async def run():
        with pytest.raises(LLMRefusalError):
            await agent_jobs.execute_agent_message_job(
                {"redis": redis}, job_id=job_id, chat_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()),
                user_token="t", content="hi", channel_id="ch-1",
            )

    asyncio.run(run())
    events = [e for e in published if e["type"] != "ping"]
    assert [e["type"] for e in events] == ["error"]
    assert events[0]["code"] == "budget_exceeded"
    status = json.loads(redis.values[f"{JOB_STATUS_PREFIX}{job_id}"])
    assert status["status"] == "failed"
    assert status["error_code"] == "budget_exceeded"
    assert status["error"].startswith("budget_exceeded: ")
