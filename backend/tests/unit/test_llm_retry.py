"""Sinas's bounded retry of an agent turn's LLM calls (issue #131).

Pinned here: a rate_limited or provider_unavailable call is made again after
the wait the provider asked for (Retry-After, or Gemini's RetryInfo
retryDelay in the error body, which the SDK doesn't read), else a backoff;
within LLM_RETRY_MAX_ATTEMPTS and LLM_RETRY_MAX_WAIT_SECONDS, and never past
the job's timeout; never for budget_exceeded or any other code; a stream
only before it showed anything (nothing is shown twice); the chat lock is
refreshed after each wait; and the tool-loop follow-up goes through it.
"""

import httpx
import openai
import pytest

from app.core.config import settings
from app.providers import OpenAIProvider
from app.providers.errors import (
    BUDGET_EXCEEDED,
    INVALID_REQUEST,
    PROVIDER_TIMEOUT,
    PROVIDER_UNAVAILABLE,
    RATE_LIMITED,
    LLMError,
    LLMRefusalError,
    error_from_exception,
)
from app.providers.openai_provider import openai_client
from app.providers.tracking import UsageTrackingProvider
from app.services import chat_steering, llm_retry
from app.services.llm_retry import RetryBudget, call_with_retry, stream_with_retry

MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.fixture
def slept(monkeypatch):
    """The waits the retry makes (instantly)."""
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(llm_retry, "_sleep", sleep)
    return waits


def _limited(after=None):
    return LLMRefusalError(RATE_LIMITED, "slow down", upstream_status=429, retry_after_seconds=after)


# ── the budget ─────────────────────────────────────────────────────────────


def test_only_rate_limits_and_unavailable_model_apis_are_retried():
    budget = RetryBudget(max_attempts=3, max_wait=90)
    assert budget.delay_for(_limited(5)) == 5
    assert budget.delay_for(LLMError(PROVIDER_UNAVAILABLE, "busy")) == 2.0  # backoff
    assert budget.delay_for(LLMRefusalError(BUDGET_EXCEEDED, "out")) is None
    assert budget.delay_for(LLMError(INVALID_REQUEST, "bad")) is None
    assert budget.delay_for(LLMError(PROVIDER_TIMEOUT, "slow")) is None  # already took the full timeout
    assert budget.delay_for(RuntimeError("boom")) is None


def test_the_budget_bounds_attempts_and_total_wait():
    budget = RetryBudget(max_attempts=3, max_wait=90)
    assert budget.delay_for(LLMError(PROVIDER_UNAVAILABLE, "busy")) == 2.0
    budget.attempts, budget.waited = 2, 2.0
    assert budget.delay_for(LLMError(PROVIDER_UNAVAILABLE, "busy")) == 4.0
    budget.attempts = 3
    assert budget.delay_for(_limited(1)) is None  # out of attempts
    # A wait that doesn't fit the total fails the call at once.
    assert RetryBudget(max_attempts=3, max_wait=90).delay_for(_limited(91)) is None
    assert RetryBudget(max_attempts=3, max_wait=90, waited=80).delay_for(_limited(11)) is None
    # LLM_RETRY_MAX_ATTEMPTS=1: no retries.
    assert RetryBudget(max_attempts=1, max_wait=90).delay_for(_limited(1)) is None


def test_the_budget_comes_from_the_settings(monkeypatch):
    monkeypatch.setattr(settings, "llm_retry_max_attempts", 5)
    monkeypatch.setattr(settings, "llm_retry_max_wait_seconds", 12.5)
    budget = RetryBudget.from_settings()
    assert (budget.max_attempts, budget.max_wait) == (5, 12.5)


async def test_a_wait_never_runs_past_the_jobs_timeout():
    with chat_steering.job_deadline(20):
        budget = RetryBudget(max_attempts=3, max_wait=90)
        assert budget.delay_for(_limited(2)) == 2  # 2s + the margin fit in 20s
        assert budget.delay_for(_limited(10)) is None  # 10s + 15s margin don't
    assert llm_retry.job_time_left() is None


# ── calls and streams ──────────────────────────────────────────────────────


async def test_a_call_is_made_again_after_the_wait_it_was_given(slept):
    calls = []

    async def call():
        calls.append(1)
        if len(calls) < 3:
            raise _limited(7)
        return "ok"

    assert await call_with_retry(call) == "ok"
    assert len(calls) == 3 and slept == [7, 7]


async def test_a_call_gives_up_after_its_attempts(slept, monkeypatch):
    monkeypatch.setattr(settings, "llm_retry_max_attempts", 3)
    calls = []

    async def call():
        calls.append(1)
        raise LLMError(PROVIDER_UNAVAILABLE, "busy")

    with pytest.raises(LLMError) as caught:
        await call_with_retry(call)
    assert caught.value.code == PROVIDER_UNAVAILABLE
    assert len(calls) == 3 and slept == [2.0, 4.0]


async def test_a_budget_refusal_is_never_retried(slept):
    calls = []

    async def call():
        calls.append(1)
        raise LLMRefusalError(BUDGET_EXCEEDED, "out of budget", upstream_status=429, retry_after_seconds=1)

    with pytest.raises(LLMRefusalError):
        await call_with_retry(call)
    assert len(calls) == 1 and slept == []

    opened = []

    def open_stream():
        opened.append(1)

        async def gen():
            raise LLMRefusalError(BUDGET_EXCEEDED, "out of budget")
            yield {}

        return gen()

    with pytest.raises(LLMRefusalError):
        [c async for c in stream_with_retry(open_stream)]
    assert len(opened) == 1 and slept == []


async def test_a_stream_is_opened_again_only_before_it_showed_anything(slept):
    opened = []

    def open_stream():
        opened.append(1)
        attempt = len(opened)

        async def gen():
            yield {"content": None, "usage": None}  # shows nothing
            if attempt == 1:
                raise _limited(3)
            yield {"content": "Hello"}

        return gen()

    chunks = [c async for c in stream_with_retry(open_stream)]
    assert [c["content"] for c in chunks if c["content"]] == ["Hello"]
    assert len(opened) == 2 and slept == [3]

    # Once it streamed content, a failure is not replayed: no duplicate output.
    opened.clear()

    def open_partial():
        opened.append(1)

        async def gen():
            yield {"content": "Hel"}
            raise _limited(1)

        return gen()

    seen = []
    with pytest.raises(LLMRefusalError):
        async for chunk in stream_with_retry(open_partial):
            seen.append(chunk)
    assert seen == [{"content": "Hel"}] and len(opened) == 1


async def test_the_chat_lock_is_refreshed_after_each_wait(slept, monkeypatch):
    refreshed = []

    async def refresh(chat_id, token=None):
        refreshed.append(chat_id)
        return True

    monkeypatch.setattr(chat_steering, "refresh_chat_lock", refresh)
    calls = []

    async def call():
        calls.append(1)
        if len(calls) == 1:
            raise _limited(1)
        return "ok"

    assert await call_with_retry(call, chat_id="chat-1") == "ok"
    assert refreshed == ["chat-1"]


# ── Gemini's retryDelay ────────────────────────────────────────────────────

_GEMINI_429 = [{
    "error": {
        "code": 429,
        "message": "You exceeded your current quota, please check your plan. Please retry in 7.4s.",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": []},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"},
        ],
    }
}]

_STREAM_OK = (
    'data: {"id":"c","object":"chat.completion.chunk","created":0,"model":"m",'
    '"choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}\n\n'
    'data: {"id":"c","object":"chat.completion.chunk","created":0,"model":"m",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    "data: [DONE]\n\n"
)


def test_geminis_retry_delay_is_read_from_the_error_body():
    request = httpx.Request("POST", "http://gemini.test/v1beta/openai/chat/completions")
    response = httpx.Response(429, request=request, json=_GEMINI_429)
    error = error_from_exception(openai.RateLimitError(message="quota", response=response, body=_GEMINI_429))
    assert error.code == RATE_LIMITED and error.retry_after_seconds == 7.0
    # Without RetryInfo, the "retry in" of the message; the headers first of all.
    body = {"error": {"code": 429, "message": "Please retry in 2.5s."}}
    error = error_from_exception(openai.RateLimitError(message="q", response=httpx.Response(429, request=request, json=body), body=body))
    assert error.retry_after_seconds == 2.5
    headed = httpx.Response(429, request=request, json=body, headers={"retry-after-ms": "1500"})
    assert error_from_exception(openai.RateLimitError(message="q", response=headed, body=body)).retry_after_seconds == 1.5


async def test_a_gemini_429_behind_the_sdk_is_retried_after_its_retry_delay(slept, monkeypatch):
    from app.providers import tracking

    async def insert(values):
        return True

    monkeypatch.setattr(tracking, "_insert_usage", insert)
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json=_GEMINI_429)
        return httpx.Response(200, text=_STREAM_OK, headers={"content-type": "text/event-stream"})

    inner = OpenAIProvider(api_key="k", base_url="http://gemini.test/v1beta/openai/")
    inner.client = openai_client(
        api_key="k", base_url="http://gemini.test/v1beta/openai/", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    provider = UsageTrackingProvider(inner, provider_name="gemini", provider_type="gemini")
    chunks = [c async for c in stream_with_retry(lambda: provider.stream(MESSAGES, model="gemini-x"))]
    assert "".join(c["content"] or "" for c in chunks) == "Hi"
    assert len(requests) == 2 and slept == [7.0]


# ── the tool-loop follow-up ────────────────────────────────────────────────


@pytest.fixture
async def chat(db, test_user):
    from app.models import Chat

    row = Chat(user_id=test_user.id, title="retry")
    db.add(row)
    await db.flush()
    return row


async def _follow_up(db, chat, user, provider, monkeypatch):
    from app.services import message_service as ms

    async def fake_provider(*_a, **_k):
        return provider

    monkeypatch.setattr(ms, "create_provider", fake_provider)
    return [
        c async for c in ms.MessageService(db)._stream_followup_after_tools(
            chat_id=str(chat.id), user_id=str(user.id), user_token="t", provider="p", model="m",
            temperature=0.7, max_tokens=None, tools=[],
        )
    ]


class _ScriptedProvider:
    """Fails its first `fail` streams with `error`, then answers."""

    def __init__(self, error, fail=1, partial=False):
        self.error, self.fail, self.partial, self.calls = error, fail, partial, 0

    async def stream(self, messages, model, tools, temperature, max_tokens):
        self.calls += 1
        if self.calls <= self.fail:
            if self.partial:
                yield {"content": "par", "tool_calls": None}
            raise self.error
        yield {"content": "answer", "tool_calls": None, "finish_reason": "stop"}


async def test_the_follow_up_after_tools_waits_out_a_rate_limit(db, chat, test_user, slept, monkeypatch):
    provider = _ScriptedProvider(_limited(4))
    chunks = await _follow_up(db, chat, test_user, provider, monkeypatch)
    assert "".join(c.get("content") or "" for c in chunks) == "answer"
    assert provider.calls == 2 and slept == [4]


async def test_the_follow_up_never_retries_a_budget_refusal(db, chat, test_user, slept, monkeypatch):
    provider = _ScriptedProvider(LLMRefusalError(BUDGET_EXCEEDED, "out"))
    with pytest.raises(LLMRefusalError):
        await _follow_up(db, chat, test_user, provider, monkeypatch)
    assert provider.calls == 1 and slept == []


async def test_the_follow_up_never_replays_a_stream_that_showed_output(db, chat, test_user, slept, monkeypatch):
    provider = _ScriptedProvider(_limited(1), partial=True)
    with pytest.raises(LLMRefusalError):
        await _follow_up(db, chat, test_user, provider, monkeypatch)
    assert provider.calls == 1 and slept == []


# ── synchronous requests give their turn their timeout as the deadline ─────


async def _sync_agent(db, user, timeout):
    import uuid

    from app.models import Agent

    agent = Agent(
        namespace="sync", name=f"deadline-{uuid.uuid4().hex[:6]}", user_id=user.id, model="m",
        is_active=True, default_job_timeout=timeout,
    )
    db.add(agent)
    await db.flush()
    return agent


def _refusing_turn(monkeypatch, seen):
    from app.services.message_service import MessageService

    async def run_turn(self, *a, **k):
        seen.append(llm_retry.job_time_left())
        # What a retry would make of a wait longer than the time left.
        seen.append(RetryBudget(max_attempts=3, max_wait=90).delay_for(_limited(60)))
        raise _limited(60)

    monkeypatch.setattr(MessageService, "run_turn", run_turn)


async def test_an_invoke_gives_its_turn_its_timeout_as_the_deadline(client, db, admin_user, monkeypatch):
    from tests.conftest import auth_headers

    agent = await _sync_agent(db, admin_user, timeout=30)
    seen = []
    _refusing_turn(monkeypatch, seen)
    r = await client.post(
        f"/agents/{agent.namespace}/{agent.name}/invoke", json={"message": "hi"}, headers=auth_headers(admin_user)
    )
    assert r.status_code == 429 and r.json()["code"] == RATE_LIMITED, r.text
    left, delay = seen
    assert 0 < left <= 30
    assert delay is None  # a 60s wait doesn't fit: the rate limit is answered at once


async def test_a_sync_webhook_gives_its_turn_its_timeout_as_the_deadline(client, db, admin_user, monkeypatch):
    import uuid

    from tests.conftest import auth_headers

    agent = await _sync_agent(db, admin_user, timeout=30)
    path = f"deadline/{uuid.uuid4().hex[:8]}"
    created = await client.post(
        "/api/v1/webhooks",
        json={
            "path": path, "target_type": "agent", "agent_namespace": agent.namespace, "agent_name": agent.name,
            "message_template": "go", "response_mode": "sync",
        },
        headers=auth_headers(admin_user),
    )
    assert created.status_code == 201, created.text
    seen = []
    _refusing_turn(monkeypatch, seen)
    r = await client.post(f"/webhooks/{path}", json={}, headers=auth_headers(admin_user))
    assert r.status_code == 429 and r.json()["code"] == RATE_LIMITED, r.text
    left, delay = seen
    assert 0 < left <= 30 and delay is None
