"""The error envelope for LLM call and agent turn failures (issues #141, #202).

Pinned here: every model API failure is classified to a stable code
(provider_auth_failed, model_not_found, provider_unavailable,
provider_timeout, context_length_exceeded, content_filtered,
invalid_request, streaming_required, provider_error, besides the refusals);
the envelope `{"detail", "code", "retryable", "upstream_status"?}` is what an
API answer, a stream `error` event (inline and queued), a job's status, a
sub-agent's result, a pipeline step and the OpenAI-compatible adapter (in
OpenAI's format) carry; a turn that can't start (no provider, no model, an
inactive provider) leaves its actionable reason in the chat and on the
stream, and an auth failure never passes on the model API's message.
"""
import asyncio
import contextlib
import json
import uuid
from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest
from sqlalchemy import select, update

from app.providers.errors import (
    BUDGET_EXCEEDED,
    CODES,
    CONTENT_FILTERED,
    CONTEXT_LENGTH_EXCEEDED,
    INTERNAL_ERROR,
    INVALID_REQUEST,
    JOB_TIMEOUT,
    MODEL_NOT_FOUND,
    PROVIDER_AUTH_FAILED,
    PROVIDER_ERROR,
    PROVIDER_NOT_CONFIGURED,
    PROVIDER_TIMEOUT,
    PROVIDER_UNAVAILABLE,
    RATE_LIMITED,
    STREAMING_REQUIRED,
    LLMError,
    LLMRefusalError,
    error_code,
    error_from_exception,
    failure_fields,
    openai_error_body,
    turn_error,
)
from tests.conftest import auth_headers

_REQUEST = httpx.Request("POST", "http://gateway.test/v1/chat/completions")


def _status_error(status, body, cls=openai.APIStatusError, headers=None):
    response = httpx.Response(status, request=_REQUEST, json=body, headers=headers or {})
    return cls(message=json.dumps(body), response=response, body=body)


# ── classification ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status, body, code",
    [
        (401, {"error": {"message": "Incorrect API key provided: sk-pr***", "type": "invalid_request_error"}}, PROVIDER_AUTH_FAILED),
        (403, {"error": {"message": "Forbidden"}}, PROVIDER_AUTH_FAILED),
        (404, {"error": {"message": "The model `gpt-x` does not exist", "code": "model_not_found"}}, MODEL_NOT_FOUND),
        (500, {"error": {"message": "Internal error"}}, PROVIDER_UNAVAILABLE),
        (503, None, PROVIDER_UNAVAILABLE),
        (529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, PROVIDER_UNAVAILABLE),
        (408, None, PROVIDER_TIMEOUT),
        (504, {"error": {"message": "upstream timed out"}}, PROVIDER_TIMEOUT),
        # OpenAI's code; Anthropic's and Gemini's messages.
        (400, {"error": {"message": "This model's maximum context length is 128000 tokens", "code": "context_length_exceeded"}}, CONTEXT_LENGTH_EXCEEDED),
        (400, {"type": "error", "error": {"type": "invalid_request_error", "message": "prompt is too long: 210000 tokens > 200000 maximum"}}, CONTEXT_LENGTH_EXCEEDED),
        (400, [{"error": {"code": 400, "message": "The input token count (1200000) exceeds the maximum number of tokens allowed (1048576).", "status": "INVALID_ARGUMENT"}}], CONTEXT_LENGTH_EXCEEDED),
        # Azure's content filter.
        (400, {"error": {"message": "The response was filtered due to the prompt triggering Azure OpenAI's content management policy.", "code": "content_filter"}}, CONTENT_FILTERED),
        (400, {"error": {"message": "max_tokens is too large", "type": "invalid_request_error"}}, INVALID_REQUEST),
        (422, {"detail": "unknown field"}, INVALID_REQUEST),
        (418, {"error": {"message": "teapot"}}, PROVIDER_ERROR),
        # The refusals keep their own classification.
        (429, {"error": {"message": "Rate limit reached"}}, RATE_LIMITED),
        (402, None, BUDGET_EXCEEDED),
    ],
)
def test_model_api_errors_get_their_code(status, body, code):
    error = error_from_exception(_status_error(status, body), provider="gw", model="m")
    assert error.code == code
    assert error.upstream_status == status
    assert str(error).startswith(f"{code}: LLM provider 'gw' ")
    assert isinstance(error, LLMRefusalError) == (code in (RATE_LIMITED, BUDGET_EXCEEDED))


def test_an_auth_failure_never_passes_on_the_model_apis_message():
    error = error_from_exception(
        _status_error(401, {"error": {"message": "Incorrect API key provided: sk-proj-abcd1234"}}), provider="gw"
    )
    assert error.code == PROVIDER_AUTH_FAILED
    assert "sk-proj" not in str(error) and "abcd1234" not in str(error)
    assert "An admin can check the provider's API key under LLM Providers" in str(error)


def test_errors_without_an_answer_get_their_code():
    timeout = openai.APITimeoutError(request=_REQUEST)
    assert error_from_exception(timeout, provider="gw").code == PROVIDER_TIMEOUT
    assert error_from_exception(anthropic.APITimeoutError(request=httpx.Request("POST", "http://x"))).code == PROVIDER_TIMEOUT
    assert error_from_exception(openai.APIConnectionError(request=_REQUEST)).code == PROVIDER_UNAVAILABLE
    assert error_from_exception(httpx.ConnectError("refused")).code == PROVIDER_UNAVAILABLE
    assert error_from_exception(httpx.ReadTimeout("slow")).code == PROVIDER_TIMEOUT
    guard = ValueError("Streaming is strongly recommended for operations that may take longer than 10 minutes.")
    assert error_from_exception(guard).code == STREAMING_REQUIRED
    # Not a model API failure: left alone.
    assert error_from_exception(ValueError("bad input")) is None
    assert error_from_exception(RuntimeError("boom")) is None
    from fastapi import HTTPException

    assert error_from_exception(HTTPException(404, "Chat not found")) is None


def test_an_error_event_in_the_middle_of_a_stream_gets_its_code():
    # Anthropic sends an `error` event after a 200 when it is overloaded.
    body = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    error = error_from_exception(_status_error(200, body))
    assert error.code == PROVIDER_UNAVAILABLE and error.upstream_status is None


# ── the envelope ───────────────────────────────────────────────────────────


def test_the_envelope():
    error = error_from_exception(
        _status_error(503, {"error": {"message": "busy"}}, headers={"retry-after": "12"}), provider="gw"
    )
    assert error.to_dict() == {
        "detail": str(error),
        "code": PROVIDER_UNAVAILABLE,
        "retryable": True,
        "upstream_status": 503,
        "retry_after": 12.0,
    }
    # No upstream answer, no upstream_status.
    assert LLMError(PROVIDER_NOT_CONFIGURED, "none").to_dict() == {
        "detail": "provider_not_configured: none", "code": PROVIDER_NOT_CONFIGURED, "retryable": False,
    }
    # The refusals' envelope is the one they had, plus `retryable`.
    assert LLMRefusalError(BUDGET_EXCEEDED, "out").to_dict() == {
        "detail": "budget_exceeded: out", "code": BUDGET_EXCEEDED, "retryable": False,
    }


@pytest.mark.parametrize(
    "code, status, retryable",
    [
        (BUDGET_EXCEEDED, 402, False), (RATE_LIMITED, 429, True), (PROVIDER_AUTH_FAILED, 502, False),
        (MODEL_NOT_FOUND, 502, False), (PROVIDER_UNAVAILABLE, 503, True), (PROVIDER_TIMEOUT, 504, True),
        (CONTEXT_LENGTH_EXCEEDED, 422, False), (CONTENT_FILTERED, 422, False), (INVALID_REQUEST, 502, False),
        (STREAMING_REQUIRED, 502, False), (PROVIDER_NOT_CONFIGURED, 503, False), (PROVIDER_ERROR, 502, False),
        (JOB_TIMEOUT, 504, False), (INTERNAL_ERROR, 500, False),
    ],
)
def test_each_codes_status_and_retryability(code, status, retryable):
    assert CODES[code] == (status, retryable)
    assert LLMError(code, "x").http_status == status


def test_an_event_round_trips_its_envelope():
    error = LLMError(PROVIDER_UNAVAILABLE, "busy", upstream_status=503, retry_after_seconds=4.0)
    event = {"type": "error", **error.event_fields()}
    assert event["error"] == event["detail"] == "provider_unavailable: busy"
    again = LLMError.from_event(event)
    assert (again.code, again.message, again.upstream_status, again.retry_after_seconds) == (
        PROVIDER_UNAVAILABLE, "busy", 503, 4.0,
    )
    assert error_code(event) == PROVIDER_UNAVAILABLE
    assert error_code({"type": "error", "error": "x"}) is None
    assert error_code({"code": "not_ours"}) is None


def test_failure_fields_of_a_job_or_sub_agent():
    assert failure_fields(LLMError(PROVIDER_UNAVAILABLE, "busy", upstream_status=503)) == {
        "error": "provider_unavailable: busy", "error_code": PROVIDER_UNAVAILABLE,
        "retryable": True, "upstream_status": 503,
    }
    raw = _status_error(404, {"error": {"message": "no such model"}})
    assert failure_fields(raw)["error_code"] == MODEL_NOT_FOUND
    assert turn_error(RuntimeError("boom")).code == INTERNAL_ERROR


# ── HTTP ───────────────────────────────────────────────────────────────────


@contextlib.asynccontextmanager
async def _raising(target, error):
    from httpx import ASGITransport, AsyncClient

    path = f"/__test_llm_error_{uuid.uuid4().hex[:6]}"

    @target.get(path)
    async def _raise():
        raise error

    try:
        async with AsyncClient(transport=ASGITransport(app=target), base_url="http://test") as client:
            yield await client.get(path)
    finally:
        target.router.routes = [r for r in target.router.routes if getattr(r, "path", None) != path]


async def test_the_api_answers_with_the_codes_status_and_the_envelope():
    from app.main import app

    error = LLMError(PROVIDER_UNAVAILABLE, "busy", upstream_status=529, retry_after_seconds=2.5)
    async with _raising(app, error) as resp:
        assert resp.status_code == 503
        assert resp.json() == {
            "detail": "provider_unavailable: busy", "code": "provider_unavailable", "retryable": True,
            "upstream_status": 529, "retry_after": 2.5,
        }
        assert resp.headers["retry-after"] == "3"
    async with _raising(app, LLMError(PROVIDER_NOT_CONFIGURED, "none")) as resp:
        assert resp.status_code == 503 and resp.json()["retryable"] is False
        assert "retry-after" not in resp.headers
    # A rate limit keeps the model API's own Retry-After.
    limited = LLMRefusalError(RATE_LIMITED, "slow down", retry_after="7")
    async with _raising(app, limited) as resp:
        assert resp.status_code == 429 and resp.headers["retry-after"] == "7"


async def test_the_openai_adapter_answers_in_openais_error_format():
    from app.main import adapters_openai_app

    error = LLMError(CONTEXT_LENGTH_EXCEEDED, "too long", upstream_status=400)
    async with _raising(adapters_openai_app, error) as resp:
        assert resp.status_code == 422
        assert resp.json() == {"error": {
            "message": "context_length_exceeded: too long", "type": "invalid_request_error", "param": None,
            "code": "context_length_exceeded", "retryable": False, "upstream_status": 400,
        }}
    assert openai_error_body(LLMRefusalError(RATE_LIMITED, "x"))["error"]["type"] == "rate_limit_error"
    assert openai_error_body(LLMError(PROVIDER_UNAVAILABLE, "x"))["error"]["type"] == "server_error"


async def test_the_openai_adapters_stream_ends_on_the_error_in_openais_format():
    from app.adapters.openai.endpoints import _agent_stream, _passthrough_stream

    def _frames(text):
        return [json.loads(line[len("data: "):]) for line in text.splitlines() if line.startswith("data: {")]

    async def _body(response):
        return "".join([p if isinstance(p, str) else p.decode() async for p in response.body_iterator])

    class _FailedTurn:
        async def send_message_stream(self, **_):
            yield {"type": "error", "content": "...", **LLMError(PROVIDER_TIMEOUT, "slow", upstream_status=504).event_fields()}

    response = await _agent_stream(_FailedTurn(), "c", "u", "t", "hi", "ns/a", "c", db=None, budget=10)
    frames = _frames(await _body(response))
    assert frames[-1] == {"error": {
        "message": "provider_timeout: slow", "type": "server_error", "param": None,
        "code": "provider_timeout", "retryable": True, "upstream_status": 504,
    }}

    class _Failing:
        async def stream(self, **_):
            yield {"content": "par"}
            raise LLMRefusalError(RATE_LIMITED, "slow down")

    frames = _frames(await _body(await _passthrough_stream(_Failing(), [], "m", {})))
    assert frames[-1]["error"]["code"] == "rate_limited"


# ── streams ────────────────────────────────────────────────────────────────


async def test_a_queued_streams_error_event_carries_the_envelope(monkeypatch):
    from app.api.runtime.endpoints import chats
    from app.services.stream_thinking import ThinkingFilter

    async def subscribe(channel_id, last_id="0", timeout=None):
        yield {"type": "error", **LLMError(PROVIDER_UNAVAILABLE, "busy", upstream_status=503).event_fields()}

    monkeypatch.setattr(chats.stream_relay, "subscribe", subscribe)
    (sse,) = [e async for e in chats._relay_one_channel("ch", "0", ThinkingFilter(False), {})]
    assert sse["event"] == "error"
    assert json.loads(sse["data"]) == {
        "error": "provider_unavailable: busy", "detail": "provider_unavailable: busy",
        "code": "provider_unavailable", "retryable": True, "upstream_status": 503,
    }


async def test_publish_failure_streams_the_envelope(monkeypatch):
    from app.services.stream_relay import stream_relay

    published = []

    async def publish(channel_id, event, ttl=None):
        published.append(event)

    monkeypatch.setattr(stream_relay, "publish", publish)
    await stream_relay.publish_failure("ch", LLMError(MODEL_NOT_FOUND, "gone", upstream_status=404))
    await stream_relay.publish_failure("ch", RuntimeError("boom"))
    assert published[0]["code"] == MODEL_NOT_FOUND and published[0]["upstream_status"] == 404
    assert published[1]["code"] == INTERNAL_ERROR and published[1]["error"] == "internal_error: boom"


class _StatusRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None, **kw):
        self.values[key] = value


def test_a_failed_turn_fails_its_job_with_the_envelope(monkeypatch):
    from app.queue import agent_jobs
    from app.services import message_service as ms
    from app.services.queue_service import JOB_STATUS_PREFIX
    from app.services.stream_relay import stream_relay

    class _Failed:
        def __init__(self, db):
            pass

        async def send_message_stream(self, **kwargs):
            error = LLMError(PROVIDER_UNAVAILABLE, "LLM provider 'gw' is unavailable", upstream_status=503)
            yield {"content": "An error occurred ...", "type": "error", **error.event_fields()}

    published = []

    async def publish(channel_id, event, ttl=None):
        published.append(event)

    async def publish_failure(channel_id, error):
        published.append({"type": "error", "again": True})

    async def publish_done(channel_id, **kw):
        published.append({"type": "done"})

    monkeypatch.setattr(ms, "MessageService", _Failed)
    monkeypatch.setattr(stream_relay, "publish", publish)
    monkeypatch.setattr(stream_relay, "publish_failure", publish_failure)
    monkeypatch.setattr(stream_relay, "publish_done", publish_done)
    redis = _StatusRedis()
    job_id = str(uuid.uuid4())

    async def run():
        with pytest.raises(LLMError):
            await agent_jobs.execute_agent_message_job(
                {"redis": redis}, job_id=job_id, chat_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()),
                user_token="t", content="hi", channel_id="ch-1",
            )

    asyncio.run(run())
    events = [e for e in published if e["type"] != "ping"]
    assert [e["type"] for e in events] == ["error"] and "again" not in events[0]
    status = json.loads(redis.values[f"{JOB_STATUS_PREFIX}{job_id}"])
    assert status["status"] == "failed"
    assert (status["error_code"], status["retryable"], status["upstream_status"]) == (PROVIDER_UNAVAILABLE, True, 503)


def test_a_follower_raises_any_coded_failure_typed():
    from app.services import sync_turns

    follower = sync_turns.RoundFollower("ch", timeout=1)
    follower.status = "failed"
    follower.error_event = {"type": "error", **LLMError(PROVIDER_TIMEOUT, "slow", upstream_status=504).event_fields()}
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

    with pytest.raises(LLMError) as caught:
        asyncio.run(run())
    assert caught.value.code == PROVIDER_TIMEOUT and caught.value.http_status == 504


def test_a_sub_agents_failure_reaches_its_parent_with_the_envelope(monkeypatch):
    import app.services.tool_execution as te
    from app.services.queue_service import queue_service
    from app.services.stream_relay import stream_relay

    sub_chat_id = uuid.uuid4()

    async def prepared(db, user_id, agent_id_str, arguments, **kwargs):
        return {
            "agent": SimpleNamespace(namespace="ns", name="helper"),
            "sub_chat": SimpleNamespace(id=sub_chat_id),
            "content": "do it",
            "depth": 1,
        }

    async def enqueue(**kwargs):
        return "job"

    async def subscribe(channel_id, last_id="0", timeout=None):
        yield {"type": "error", **LLMError(PROVIDER_NOT_CONFIGURED, "No LLM provider is configured.").event_fields()}

    monkeypatch.setattr(te, "prepare_agent_delegation", prepared)
    monkeypatch.setattr(queue_service, "enqueue_agent_message", enqueue)
    monkeypatch.setattr(stream_relay, "subscribe", subscribe)
    result = asyncio.run(te.execute_agent_tool(
        db=None, chat=SimpleNamespace(id=uuid.uuid4()), user_id="u", user_token="t",
        agent_id_str="ns__helper", arguments={"prompt": "x"}, create_chat_with_agent_fn=None,
    ))
    assert result["error_code"] == PROVIDER_NOT_CONFIGURED and result["retryable"] is False
    assert result["error"].startswith("provider_not_configured: ")
    assert result["chat_id"] == str(sub_chat_id)


async def test_a_pipeline_agent_step_keeps_the_code(db, test_user, monkeypatch):
    from app.models.agent import Agent
    from app.services import pipeline_runner as pr
    from app.services.queue_service import queue_service
    from app.services.stream_relay import stream_relay

    agent = Agent(namespace="t", name=f"step-{uuid.uuid4().hex[:6]}", user_id=test_user.id)
    db.add(agent)
    await db.flush()

    @contextlib.asynccontextmanager
    async def session():
        yield db

    async def enqueue(**kwargs):
        return "job"

    async def subscribe(channel_id, last_id="0", timeout=None):
        yield {"type": "error", **LLMRefusalError(RATE_LIMITED, "slow down", upstream_status=429).event_fields()}

    monkeypatch.setattr(pr, "AsyncSessionLocal", session)
    monkeypatch.setattr(queue_service, "enqueue_agent_message", enqueue)
    monkeypatch.setattr(stream_relay, "subscribe", subscribe)
    with pytest.raises(pr.StepError) as caught:
        await pr._execute_agent_step(
            {"agent": f"{agent.namespace}/{agent.name}"}, {}, user_id=str(test_user.id), user_token="t",
            run_id="r", agent_depth=0, pipeline_label="p", summary={},
        )
    assert caught.value.code == RATE_LIMITED


# ── a turn that can't start (#202) ─────────────────────────────────────────


async def _agent_chat(db, user, **agent_fields):
    from app.models import Agent, Chat

    agent = Agent(namespace="t", name=f"noprov-{uuid.uuid4().hex[:6]}", user_id=user.id, **agent_fields)
    db.add(agent)
    await db.flush()
    chat = Chat(
        user_id=user.id, agent_id=agent.id, agent_namespace=agent.namespace, agent_name=agent.name, title="t",
    )
    db.add(chat)
    await db.flush()
    return agent, chat


async def _transcript(db, chat):
    from app.models import Message

    rows = (
        await db.execute(select(Message).where(Message.chat_id == chat.id).order_by(Message.created_at))
    ).scalars().all()
    return [(m.role, m.content) for m in rows]


@pytest.fixture
async def no_default_provider(db):
    from app.models import LLMProvider

    await db.execute(update(LLMProvider).values(is_default=False))


async def test_a_turn_without_a_provider_says_why_in_the_chat_and_the_stream(db, test_user, no_default_provider):
    from app.services.message_service import MessageService

    _, chat = await _agent_chat(db, test_user, model="m")
    events = [
        e async for e in MessageService(db).send_message_stream(
            chat_id=str(chat.id), user_id=str(test_user.id), user_token="t", content="hello"
        )
    ]
    (error,) = [e for e in events if e.get("type") == "error"]
    assert error["code"] == PROVIDER_NOT_CONFIGURED and error["retryable"] is False
    assert "An admin can add one under LLM Providers" in error["error"]
    transcript = await _transcript(db, chat)
    assert transcript[0] == ("user", "hello")
    role, content = transcript[-1]
    assert role == "assistant"
    assert content.startswith("The agent could not process your message.")
    assert "provider_not_configured: No LLM provider is configured" in content


async def test_an_inactive_provider_is_named_with_what_to_do(db, test_user, no_default_provider):
    from app.models import LLMProvider
    from app.services.message_service import MessageService

    provider = LLMProvider(name=f"off-{uuid.uuid4().hex[:6]}", provider_type="openai", is_active=False)
    db.add(provider)
    await db.flush()
    _, chat = await _agent_chat(db, test_user, model="m", llm_provider_id=provider.id)
    events = [
        e async for e in MessageService(db).send_message_stream(
            chat_id=str(chat.id), user_id=str(test_user.id), user_token="t", content="hello"
        )
    ]
    (error,) = [e for e in events if e.get("type") == "error"]
    assert error["code"] == PROVIDER_NOT_CONFIGURED
    assert provider.name in error["error"] and "under LLM Providers" in error["error"]
    assert (await _transcript(db, chat))[-1][1].startswith("The agent could not process your message.")


async def test_a_turn_without_a_model_says_so(db, test_user):
    from app.models import LLMProvider
    from app.services.message_service import MessageService

    provider = LLMProvider(name=f"nomodel-{uuid.uuid4().hex[:6]}", provider_type="ollama", is_active=True)
    db.add(provider)
    await db.flush()
    _, chat = await _agent_chat(db, test_user, llm_provider_id=provider.id)
    events = [
        e async for e in MessageService(db).send_message_stream(
            chat_id=str(chat.id), user_id=str(test_user.id), user_token="t", content="hello"
        )
    ]
    (error,) = [e for e in events if e.get("type") == "error"]
    assert error["code"] == PROVIDER_NOT_CONFIGURED and "No model is set" in error["error"]


async def test_a_provider_that_cant_be_set_up_says_so_without_the_details(db, test_user):
    from app.models import LLMProvider
    from app.services.message_service import MessageService

    # An OpenAI provider without an API key: the SDK refuses to build a client.
    provider = LLMProvider(name=f"nokey-{uuid.uuid4().hex[:6]}", provider_type="openai", is_active=True)
    db.add(provider)
    await db.flush()
    _, chat = await _agent_chat(db, test_user, model="m", llm_provider_id=provider.id)
    events = [
        e async for e in MessageService(db).send_message_stream(
            chat_id=str(chat.id), user_id=str(test_user.id), user_token="t", content="hello"
        )
    ]
    (error,) = [e for e in events if e.get("type") == "error"]
    assert error["code"] == PROVIDER_NOT_CONFIGURED
    assert f"LLM provider '{provider.name}' could not be set up" in error["error"]
    assert "OPENAI_API_KEY" not in error["error"]


async def test_the_inline_stream_sends_the_reason_as_an_error_event(client, db, admin_user, no_default_provider):
    _, chat = await _agent_chat(db, admin_user, model="m")
    r = await client.post(f"/chats/{chat.id}/messages/stream", json={"content": "hi"}, headers=auth_headers(admin_user))
    assert r.status_code == 200, r.text
    lines = r.text.splitlines()
    events = [line[len("event: "):].strip() for line in lines if line.startswith("event: ")]
    assert "error" in events and "done" not in events
    (data,) = [json.loads(line[len("data: "):]) for line in lines if line.startswith("data: ") and "provider_not_configured" in line]
    assert data["code"] == PROVIDER_NOT_CONFIGURED and data["retryable"] is False
    assert "An admin can add one under LLM Providers" in data["error"]


async def test_a_non_streaming_send_answers_503_and_keeps_the_reason(client, db, admin_user, no_default_provider):
    _, chat = await _agent_chat(db, admin_user, model="m")
    r = await client.post(f"/chats/{chat.id}/messages", json={"content": "hi"}, headers=auth_headers(admin_user))
    assert r.status_code == 503, r.text
    assert r.json()["code"] == PROVIDER_NOT_CONFIGURED and r.json()["retryable"] is False
    assert (await _transcript(db, chat))[-1][1].startswith("The agent could not process your message.")
