"""Identity headers on LLM calls: an LLM provider's `config.headers`.

Pinned here: templates are static text plus a fixed variable set, checked
when the provider is written (names, reserved headers, variables, characters);
a header is sent whole or not at all (a variable without a safe value leaves
it out, the call still goes through); values can't inject headers; every
provider type sends them on every call (complete, stream, batch submission),
over any other header of the same name, with the trace-context guard intact;
the values come from the call's identity (the chat's user, agent, chat, the
API key the run acts through), never from inbound request headers; and the
identity is resolved again wherever a turn continues (a queued job, a
resume), from what that job carries.

No network calls: providers talk to an in-memory transport.
"""
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import anthropic
import httpx
import httpx2
import openai
import pytest
from sqlalchemy import select

from app.core.auth import bind_api_key, create_api_key, reset_api_key
from app.core.config import settings
from app.core.telemetry import attached, extract_trace_context
from app.models import LLMProvider
from app.providers import (
    AnthropicProvider,
    AzureOpenAIProvider,
    GeminiProvider,
    MistralProvider,
    OllamaProvider,
    OpenAIProvider,
    create_provider,
    ollama_provider,
)
from app.providers.anthropic_provider import anthropic_client
from app.providers.attribution import (
    TEMPLATE_VARIABLES,
    clean_value,
    merge_headers,
    render_headers,
    resolve_attribution,
    validate_header_templates,
)
from app.providers.clients import BUDGET_RULES_HEADER, CONTEXT_OPT_IN_HEADER
from app.providers.openai_provider import openai_client
from app.schemas.spec.llm_provider import LLMProviderSpec

MESSAGES = [{"role": "user", "content": "hi"}]
TRACEPARENT = "00-4c79f60c11214eb38604f4ae0781bfb2-b2a7c1d9e8f30411-01"

VARIABLES = {
    "user.id": "u-1",
    "user.email": "ann@example.com",
    "user.custom_fields": {"orgId": "org-42", "tier": 3, "flag": True, "nested": {"a": 1}},
    "agent.namespace": "research",
    "agent.name": "analyst",
    "chat.id": "c-9",
    "api_key.name": "run-123",
}


# ── validation ──────────────────────────────────────────────────────────────


def test_a_valid_set_of_templates_is_kept_as_written():
    headers = {
        "X-Customer-Id": "{{user.custom_fields.orgId}}",
        "x-tags": "agent:{{ agent.namespace }}/{{agent.name}},chat:{{chat.id}}",
        "x-static": "sinas",
        "x-meta": '{"user": "{{user.id}}", "key": "{{api_key.name}}"}',
        "OpenAI-Project": "proj_123",
    }
    assert validate_header_templates(headers) == headers
    assert validate_header_templates(None) == {}


@pytest.mark.parametrize(
    "headers, problem",
    [
        ({"bad name": "x"}, "invalid"),
        ({"x_under": "x"}, "invalid"),
        ({"x-a": "1", "X-A": "2"}, "twice"),
        ({"x-a": ""}, "empty"),
        ({"x-a": 5}, "string"),
        ({"x-a": "line\r\nInjected: yes"}, "printable"),
        ({"x-a": "café"}, "printable"),
        ({"x-a": "{{user.password}}"}, "unknown variable"),
        ({"x-a": "{{request.headers.x}}"}, "unknown variable"),
        ({"x-a": "{{user.custom_fields.a-b}}"}, "field key"),
        ({"x-a": "{{attribution.a.b}}"}, "field key"),
        ({"x-a": "{{user.id"}, "unbalanced"),
        ({"x-a": "{{ user.id }}}}"}, "unbalanced"),
        ({"x-a": "x" * 1025}, "longer"),
        ({f"x-{i}": "v" for i in range(21)}, "at most"),
        ("x-a: 1", "object"),
    ],
)
def test_invalid_templates_are_refused(headers, problem):
    with pytest.raises(ValueError, match=problem):
        validate_header_templates(headers)


@pytest.mark.parametrize(
    "name",
    [
        "Authorization", "x-api-key", "api-key", "Host", "Content-Length", "Cookie",
        "traceparent", "TraceState", "baggage", "b3", "x-b3-traceid", "uber-trace-id",
        "anthropic-version", "anthropic-beta", "x-stainless-arch", "x-goog-api-key",
        "x-sinas-propagate-context", BUDGET_RULES_HEADER, "proxy-authorization",
        "x-should-retry", "user-agent", "ocp-apim-subscription-key",
    ],
)
def test_reserved_headers_are_refused(name):
    with pytest.raises(ValueError, match="reserved"):
        validate_header_templates({name: "{{user.id}}"})


def test_the_provider_spec_checks_its_headers():
    spec = LLMProviderSpec(
        name="gw", provider_type="openai",
        config={"headers": {"x-customer-id": "{{user.custom_fields.orgId}}"}, "propagate_context": True},
    )
    assert spec.config["headers"] == {"x-customer-id": "{{user.custom_fields.orgId}}"}
    with pytest.raises(ValueError, match="reserved"):
        LLMProviderSpec(name="gw", provider_type="openai", config={"headers": {"Authorization": "x"}})
    # An empty set is no setting at all.
    assert "headers" not in LLMProviderSpec(name="gw", provider_type="openai", config={"headers": {}}).config


def test_every_documented_variable_is_accepted():
    for variable in TEMPLATE_VARIABLES:
        name = variable.replace("<key>", "orgId")
        assert validate_header_templates({"x-v": "{{" + name + "}}"})


# ── rendering ───────────────────────────────────────────────────────────────


def test_templates_render_from_the_variables():
    out = render_headers(
        {
            "x-customer-id": "{{user.custom_fields.orgId}}",
            "x-tags": "agent:{{agent.namespace}}/{{agent.name}}",
            "x-tier": "{{user.custom_fields.tier}}",
            "x-flag": "{{user.custom_fields.flag}}",
            "x-static": "sinas",
            "x-meta": '{"key":"{{api_key.name}}"}',
        },
        VARIABLES,
    )
    assert out == {
        "x-customer-id": "org-42",
        "x-tags": "agent:research/analyst",
        "x-tier": "3",
        "x-flag": "true",
        "x-static": "sinas",
        "x-meta": '{"key":"run-123"}',
    }


def test_a_header_with_any_unresolved_variable_is_left_out_whole():
    out = render_headers(
        {
            "x-org": "org-{{user.custom_fields.missing}}",  # never "org-"
            "x-nested": "{{user.custom_fields.nested}}",  # not a scalar
            "x-key": "{{api_key.name}}",
            "x-ok": "{{user.id}}",
        },
        {**VARIABLES, "api_key.name": None},
    )
    assert out == {"x-ok": "u-1"}


@pytest.mark.parametrize(
    "value",
    [
        "org\r\nX-Injected: 1", "org\nX", "org\x00", "tab\there", "quote\"d", "back\\slash",
        "café", "", "   ", "x" * 1025, ["a"], {"a": 1}, None,
    ],
)
def test_an_unsafe_value_is_no_value(value):
    assert clean_value(value) is None
    out = render_headers({"x-org": "{{user.custom_fields.orgId}}"}, {"user.custom_fields": {"orgId": value}})
    assert out == {}


def test_a_value_is_never_cut_to_fit():
    long = "a" * 1000
    out = render_headers({"x-a": "{{user.id}}-{{user.id}}"}, {"user.id": long})
    assert out == {}  # 2001 characters: left out, not truncated


def test_a_stored_template_that_no_longer_validates_is_ignored():
    assert render_headers({"Authorization": "{{user.id}}"}, VARIABLES) == {}


def test_a_later_header_replaces_an_earlier_one_whatever_its_case():
    merged = merge_headers({"X-Customer-Id": "spoofed", "keep": "1"}, {"x-customer-id": "org-42"})
    assert merged == {"keep": "1", "x-customer-id": "org-42"}


# ── on the wire: every provider type ───────────────────────────────────────

_HttpxClient = httpx.AsyncClient
_ENDED = (openai.BadRequestError, anthropic.BadRequestError, httpx.HTTPStatusError)


class _Wire:
    def __init__(self, answers=None):
        self.sent = []
        self.answers = answers or {}

    def _answer(self, request, response_class):
        self.sent.append(request)
        for suffix, body in self.answers.items():
            if request.url.path.endswith(suffix):
                return response_class(200, json=body)
        return response_class(400, json={"error": {"message": "refused by the test"}})

    def client(self, **kwargs):
        return _HttpxClient(
            transport=httpx.MockTransport(lambda r: self._answer(r, httpx.Response)), **kwargs
        )

    def client2(self):
        return httpx2.AsyncClient(transport=httpx2.MockTransport(lambda r: self._answer(r, httpx2.Response)))


def _openai_like(cls, wire, **kw):
    provider = cls(api_key="k", base_url="http://gateway.test/v1")
    provider.client = openai_client(
        api_key="k", base_url="http://gateway.test/v1", http_client=wire.client(), max_retries=0
    )
    return provider


def _azure(wire, mp):
    provider = AzureOpenAIProvider(
        api_key="k", azure_endpoint="http://gateway.test",
        # A header extra_params would also send: the template's value wins.
        extra_params={"extra_headers": {"X-Customer-Id": "from-extra-params"}},
    )
    provider.client = openai_client(
        openai.AsyncAzureOpenAI, api_key="k", api_version=provider.api_version,
        azure_endpoint="http://gateway.test", http_client=wire.client(), max_retries=0,
    )
    return provider


def _anthropic(wire, mp):
    provider = AnthropicProvider(api_key="k", base_url="http://gateway.test")
    provider.client = anthropic_client(
        api_key="k", base_url="http://gateway.test", http_client=wire.client2(), max_retries=0
    )
    return provider


def _ollama(wire, mp):
    mp.setattr(ollama_provider.httpx, "AsyncClient", wire.client)
    return OllamaProvider(base_url="http://gateway.test")


PROVIDERS = {
    "openai": lambda wire, mp: _openai_like(OpenAIProvider, wire),
    "azure": _azure,
    "gemini": lambda wire, mp: _openai_like(GeminiProvider, wire),
    "mistral": lambda wire, mp: _openai_like(MistralProvider, wire),
    "anthropic": _anthropic,
    "ollama": _ollama,
}
IDENTITY = {"x-customer-id": "org-42", "x-tags": "agent:research/analyst"}


async def _complete(provider):
    await provider.complete(MESSAGES, model="m", max_tokens=1, extra_headers={"x-customer-id": "kwarg"})


async def _stream(provider):
    async for _ in provider.stream(MESSAGES, model="m", max_tokens=1):
        pass


def _run(provider, call, *, traced=False):
    async def go():
        ctx = extract_trace_context({"traceparent": TRACEPARENT}) if traced else None
        with attached(ctx):
            try:
                await call(provider)
            except _ENDED:
                pass

    asyncio.run(go())


@pytest.mark.parametrize("call", [_complete, _stream], ids=["complete", "stream"])
@pytest.mark.parametrize("kind", PROVIDERS)
def test_every_provider_type_sends_its_identity_headers(kind, call, monkeypatch):
    if kind == "ollama" and call is _complete:
        call = lambda p: p.complete(MESSAGES, model="m", max_tokens=1)  # noqa: E731 - no kwargs
    wire = _Wire()
    provider = PROVIDERS[kind](wire, monkeypatch)
    provider.identity_headers = dict(IDENTITY)
    provider.budget_rules = [{"status": 403, "message_contains": "spend cap"}]
    _run(provider, call)
    assert wire.sent
    for request in wire.sent:
        # Exactly one value per name, the template's, over extra_params and
        # any extra_headers a caller passed.
        assert request.headers.get_list("x-customer-id") == ["org-42"]
        assert request.headers["x-tags"] == "agent:research/analyst"
        # Private markers never leave the process.
        assert BUDGET_RULES_HEADER not in request.headers
        assert CONTEXT_OPT_IN_HEADER not in request.headers


@pytest.mark.parametrize("kind", PROVIDERS)
def test_the_trace_context_guard_still_holds(kind, monkeypatch):
    wire = _Wire()
    provider = PROVIDERS[kind](wire, monkeypatch)
    provider.identity_headers = dict(IDENTITY)
    _run(provider, _stream, traced=True)
    assert wire.sent
    assert not any("traceparent" in r.headers for r in wire.sent)
    assert all(r.headers["x-customer-id"] == "org-42" for r in wire.sent)

    wire = _Wire()
    provider = PROVIDERS[kind](wire, monkeypatch)
    provider.identity_headers = dict(IDENTITY)
    provider.propagate_context = True
    _run(provider, _stream, traced=True)
    assert all(r.headers["traceparent"] == TRACEPARENT for r in wire.sent)
    assert all(r.headers["x-customer-id"] == "org-42" for r in wire.sent)
    assert not any(CONTEXT_OPT_IN_HEADER in r.headers for r in wire.sent)


BATCH_REQUEST = {"custom_id": "r-1", "model": "m", "messages": MESSAGES, "max_tokens": 1}


def test_an_openai_batch_submission_carries_them_on_its_calls_not_in_its_items():
    uploaded = {
        "id": "file-1", "object": "file", "bytes": 1, "created_at": 0,
        "filename": "batch.jsonl", "purpose": "batch", "status": "processed",
    }
    wire = _Wire({"/files": uploaded})
    provider = _openai_like(OpenAIProvider, wire)
    provider.identity_headers = dict(IDENTITY)
    _run(provider, lambda p: p.submit_batch([BATCH_REQUEST]))
    upload, create = wire.sent
    assert upload.headers["x-customer-id"] == "org-42" and create.headers["x-customer-id"] == "org-42"
    line = next(part for part in upload.content.split(b"\r\n") if part.startswith(b'{"custom_id"'))
    assert "x-customer-id" not in json.dumps(json.loads(line))


def test_an_anthropic_batch_submission_carries_them():
    wire = _Wire()
    provider = _anthropic(wire, None)
    provider.identity_headers = dict(IDENTITY)
    _run(provider, lambda p: p.submit_batch([BATCH_REQUEST]))
    (create,) = wire.sent
    assert create.headers["x-customer-id"] == "org-42"


# ── identity: where the values come from ────────────────────────────────────


TEMPLATES = {
    "x-customer-id": "{{user.custom_fields.orgId}}",
    "x-user": "{{user.email}}",
    "x-run": "{{api_key.name}}",
    "x-chat": "{{chat.id}}",
    "x-agent": "{{agent.namespace}}/{{agent.name}}",
}


async def _user(db, custom_fields):
    from app.models.user import User

    user = User(email=f"attr-{uuid.uuid4().hex[:8]}@example.com", custom_fields=custom_fields)
    db.add(user)
    await db.flush()
    return user


async def test_values_come_from_the_calls_identity(db):
    user = await _user(db, {"orgId": "org-42"})
    key, _ = await create_api_key(db, user, "run-123", {})
    context = {"user_id": str(user.id), "chat_id": "c-9", "agent": "research/analyst"}

    token = bind_api_key(str(key.id))
    try:
        headers, _ = await resolve_attribution(db, context, TEMPLATES)
    finally:
        reset_api_key(token)
    assert headers == {
        "x-customer-id": "org-42",
        "x-user": user.email,
        "x-run": "run-123",
        "x-chat": "c-9",
        "x-agent": "research/analyst",
    }

    # Without an acting key (a console user, a scheduled run): no x-run.
    headers, _ = await resolve_attribution(db, context, TEMPLATES)
    assert "x-run" not in headers and headers["x-customer-id"] == "org-42"


async def test_only_the_users_own_live_key_names_the_run(db):
    user = await _user(db, {"orgId": "org-42"})
    other = await _user(db, {})
    others_key, _ = await create_api_key(db, other, "not-yours", {})
    expired, _ = await create_api_key(db, user, "expired", {})
    expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.flush()
    context = {"user_id": str(user.id)}
    for key in (others_key, expired):
        token = bind_api_key(str(key.id))
        try:
            headers, _ = await resolve_attribution(db, context, {"x-run": "{{api_key.name}}"})
        finally:
            reset_api_key(token)
        assert headers == {}


async def test_a_shared_service_keys_calls_carry_no_customer_and_still_go_through(db):
    # Platform work (ingestion through a service account without the field):
    # the header is left out, nothing fails.
    service = await _user(db, {})
    headers, customer = await resolve_attribution(
        db, {"user_id": str(service.id), "agent": "ingest/extract"}, {"x-customer-id": "{{user.custom_fields.orgId}}"}
    )
    assert headers == {} and customer is None


async def test_the_customer_is_recorded_from_the_configured_field(db, monkeypatch):
    user = await _user(db, {"orgId": "org-42"})
    monkeypatch.setattr(settings, "usage_customer_field", "orgId")
    _, customer = await resolve_attribution(db, {"user_id": str(user.id)}, None)
    assert customer == "org-42"
    monkeypatch.setattr(settings, "usage_customer_field", None)
    assert await resolve_attribution(db, {"user_id": str(user.id)}, None) == ({}, None)


async def _chat(db, user, attribution):
    from app.models.chat import Chat

    chat = Chat(user_id=user.id, title="t", attribution=attribution)
    db.add(chat)
    await db.flush()
    return chat


async def test_a_chat_is_attributed_by_its_snapshot_whoever_continues_it(db, monkeypatch):
    from app.providers.attribution import new_chat_attribution

    monkeypatch.setattr(settings, "attribution_fields", "orgId, costCenter, not-a-key")
    monkeypatch.setattr(settings, "usage_customer_field", "orgId")
    user = await _user(db, {"orgId": "org-1", "costCenter": 7, "department": "r&d", "nested": {"a": 1}})

    # Taken at creation: the configured fields only, with safe values.
    snapshot = await new_chat_attribution(db, user.id)
    assert snapshot == {"orgId": "org-1", "costCenter": 7}
    chat = await _chat(db, user, snapshot)

    # The user's fields change later (a sign-in for another organization):
    # the chat keeps its snapshot; {{user.custom_fields.*}} reads them live.
    user.custom_fields = {"orgId": "org-2"}
    await db.flush()
    templates = {"x-org": "{{attribution.orgId}}", "x-now": "{{user.custom_fields.orgId}}"}
    headers, customer = await resolve_attribution(
        db, {"user_id": str(user.id), "chat_id": str(chat.id)}, templates
    )
    assert headers == {"x-org": "org-1", "x-now": "org-2"}
    assert customer == "org-1"

    # A sub-agent's chat inherits its parent's snapshot.
    assert await new_chat_attribution(db, user.id, parent_chat_id=chat.id) == snapshot
    # A new chat takes the fields as they are now.
    assert await new_chat_attribution(db, user.id) == {"orgId": "org-2"}


async def test_a_chat_without_a_snapshot_takes_one_at_its_next_call(db, monkeypatch):
    monkeypatch.setattr(settings, "attribution_fields", "orgId")
    user = await _user(db, {"orgId": "org-1"})
    chat = await _chat(db, user, None)  # created before attribution was configured
    headers, _ = await resolve_attribution(
        db, {"user_id": str(user.id), "chat_id": str(chat.id)}, {"x-org": "{{attribution.orgId}}"}
    )
    assert headers == {"x-org": "org-1"}
    assert chat.attribution == {"orgId": "org-1"}  # kept from now on

    user.custom_fields = {"orgId": "org-2"}
    headers, _ = await resolve_attribution(
        db, {"user_id": str(user.id), "chat_id": str(chat.id)}, {"x-org": "{{attribution.orgId}}"}
    )
    assert headers == {"x-org": "org-1"}


async def test_without_attribution_fields_no_snapshot_is_taken(db, monkeypatch):
    from app.providers.attribution import new_chat_attribution

    monkeypatch.setattr(settings, "attribution_fields", None)
    monkeypatch.setattr(settings, "usage_customer_field", None)
    user = await _user(db, {"orgId": "org-1"})
    assert await new_chat_attribution(db, user.id) is None
    chat = await _chat(db, user, None)
    headers, customer = await resolve_attribution(
        db, {"user_id": str(user.id), "chat_id": str(chat.id)}, {"x-org": "{{attribution.orgId}}"}
    )
    assert headers == {} and customer is None and chat.attribution is None


async def test_a_malformed_stored_template_never_stops_the_call(db, monkeypatch):
    monkeypatch.setattr(settings, "usage_customer_field", None)
    user = await _user(db, {"orgId": "org-1"})
    headers, customer = await resolve_attribution(
        db, {"user_id": str(user.id)}, {"x-customer-id": None, "x-ok": "{{user.id}}"}
    )
    assert headers == {} and customer is None  # the whole set is ignored, no exception


def test_the_customer_field_is_always_in_the_snapshot(monkeypatch):
    from app.providers.attribution import attribution_keys

    monkeypatch.setattr(settings, "attribution_fields", ",".join(f"f{i}" for i in range(25)))
    monkeypatch.setattr(settings, "usage_customer_field", "orgId")
    keys = attribution_keys()
    assert keys[0] == "orgId" and len(keys) == 20


async def test_a_failed_lookup_sends_nothing_and_never_raises(db):
    headers, customer = await resolve_attribution(db, {"user_id": "not-a-uuid"}, TEMPLATES)
    assert headers == {} and customer is None


async def _provider_row(db, headers):
    row = LLMProvider(
        name=f"gw-{uuid.uuid4().hex[:8]}", provider_type="ollama", api_endpoint="http://gateway.test",
        config={"headers": headers}, is_active=True,
    )
    db.add(row)
    await db.flush()
    return row


async def test_create_provider_resolves_them_for_each_call(db, monkeypatch):
    user = await _user(db, {"orgId": "org-42"})
    row = await _provider_row(db, {"x-customer-id": "{{user.custom_fields.orgId}}"})
    monkeypatch.setattr(settings, "usage_customer_field", "orgId")
    provider = await create_provider(
        row.name, "m", db, usage_context={"user_id": str(user.id), "chat_id": "c-1", "source": "chat"}
    )
    assert provider._inner.identity_headers == {"x-customer-id": "org-42"}
    assert provider._context["customer"] == "org-42"

    # A call without a user (none to attribute) gets no identity headers.
    provider = await create_provider(row.name, "m", db)
    assert provider._inner.identity_headers == {}


async def test_a_queued_job_resolves_the_key_it_was_enqueued_with(db, monkeypatch):
    """Queued turns, resumes (approval, ask_user, delegate) and wakeups all run
    under `_acting_as_job_key` with the api_key_id they were enqueued with
    (queue_service, deferred_completions, chat_wakeups): the provider built
    inside sees that key, so {{api_key.name}} holds in every one of them."""
    from app.queue import agent_jobs

    user = await _user(db, {"orgId": "org-42"})
    key, _ = await create_api_key(db, user, "run-123", {})
    row = await _provider_row(db, {"x-run": "{{api_key.name}}", "x-customer-id": "{{user.custom_fields.orgId}}"})
    seen = {}

    @agent_jobs._acting_as_job_key
    async def job(ctx, **kwargs):
        provider = await create_provider(row.name, "m", db, usage_context={"user_id": kwargs["user_id"]})
        seen.update(provider._inner.identity_headers)

    await job({}, user_id=str(user.id), api_key_id=str(key.id))
    assert seen == {"x-run": "run-123", "x-customer-id": "org-42"}


async def test_a_queued_turn_records_the_acting_key_at_enqueue(monkeypatch):
    """What `_acting_as_job_key` restores is captured at enqueue: a queued
    turn (and a sub-agent delegation, which enqueues one the same way)
    records the key the enqueuing request acts through."""
    from app.services import queue_service as qs

    captured = []

    class _Pool:
        async def enqueue_job(self, name, **kwargs):
            captured.append((name, kwargs.get("api_key_id")))

    async def _pool():
        return _Pool()

    monkeypatch.setattr(qs, "get_arq_pool", _pool)
    key_id = str(uuid.uuid4())
    token = bind_api_key(key_id)
    try:
        await qs.queue_service.enqueue_agent_message(
            chat_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()), user_token="t",
            content="hi", channel_id=str(uuid.uuid4()),
        )
    finally:
        reset_api_key(token)
    assert captured == [("execute_agent_message_job", key_id)]


def test_no_inbound_request_header_reaches_a_provider():
    """Nothing forwards a caller's request headers to a model API: the only
    headers a provider adds are its own (`identity_headers`, trace context),
    and no call site passes `extra_headers` from a request."""
    import pathlib

    import app

    root = pathlib.Path(app.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        if "providers" in path.parts:
            continue
        text = path.read_text()
        if "extra_headers" in text or "identity_headers" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == []
