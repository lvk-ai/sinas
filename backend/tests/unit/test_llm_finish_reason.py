"""Why each LLM call stopped, on its llm_usage row (issue #140).

Pinned here: every provider's finish/stop reason (OpenAI, Azure, Gemini,
Mistral, Anthropic, Ollama; complete and stream; provider batch results)
reaches the usage row, normalized to stop / length / tool_calls /
content_filter / error / other, with the provider's own value beside it; a
failed call is "error"; /usage/calls returns both (added fields only); the
migration leaves one head.
"""
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.providers import tracking
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.azure_openai_provider import AzureOpenAIProvider
from app.providers.gemini_provider import GeminiProvider
from app.providers.mistral_provider import MistralProvider
from app.providers.ollama_provider import OllamaProvider
from app.providers.openai_provider import OpenAIProvider, openai_client
from app.providers.tracking import UsageTrackingProvider, normalize_finish_reason

MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.fixture
def recorded(monkeypatch):
    rows = []

    async def insert(values):
        rows.append(values)
        return True

    monkeypatch.setattr(tracking, "_insert_usage", insert)
    return rows


async def _settled():
    import asyncio

    await asyncio.sleep(0)


@pytest.mark.parametrize(
    "raw, normalized",
    [
        ("stop", "stop"), ("end_turn", "stop"), ("stop_sequence", "stop"),
        ("length", "length"), ("max_tokens", "length"), ("model_length", "length"),
        ("MAX_TOKENS", "length"), ("FINISH_REASON_MAX_TOKENS", "length"),
        ("tool_calls", "tool_calls"), ("tool_use", "tool_calls"), ("function_call", "tool_calls"),
        ("content_filter", "content_filter"), ("refusal", "content_filter"), ("SAFETY", "content_filter"),
        ("error", "error"), ("pause_turn", "other"), ("load", "other"),
        (None, None), ("", None),
    ],
)
def test_finish_reasons_are_normalized(raw, normalized):
    assert normalize_finish_reason(raw) == normalized


# ── OpenAI-compatible providers (OpenAI, Azure, Gemini, Mistral) ───────────


def _completion(finish):
    return {
        "id": "c", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi"}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }


def _sse(finish):
    chunks = [
        {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
         "choices": [{"index": 0, "delta": {"content": "Hi"}, "finish_reason": None}]},
        {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
         "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
    ]
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def _wired(provider, finish):
    def handler(request):
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, text=_sse(finish), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=_completion(finish))

    provider.client = openai_client(
        api_key="k", base_url="http://llm.test/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return provider


_OPENAI_COMPATIBLE = {
    "openai": lambda: OpenAIProvider(api_key="k"),
    "azure": lambda: AzureOpenAIProvider(api_key="k", azure_endpoint="http://llm.test"),
    "gemini": lambda: GeminiProvider(api_key="k"),
    "mistral": lambda: MistralProvider(api_key="k"),
}


@pytest.mark.parametrize("kind", sorted(_OPENAI_COMPATIBLE))
@pytest.mark.parametrize("finish, normalized", [("length", "length"), ("content_filter", "content_filter")])
async def test_openai_compatible_providers_record_the_finish_reason(kind, finish, normalized, recorded):
    provider = UsageTrackingProvider(_wired(_OPENAI_COMPATIBLE[kind](), finish), provider_name=kind, provider_type=kind)
    await provider.complete(MESSAGES, model="m")
    [c async for c in provider.stream(MESSAGES, model="m")]
    await _settled()
    assert [(r["streamed"], r["finish_reason"], r["finish_reason_raw"]) for r in recorded] == [
        (False, normalized, finish), (True, normalized, finish),
    ]


# ── Anthropic ──────────────────────────────────────────────────────────────


class _FakeStream:
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for item in self._items:
            yield item

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


_ANTHROPIC_USAGE = SimpleNamespace(
    input_tokens=3, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0,
)


async def test_anthropic_records_the_stop_reason(recorded):
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="Hi")], stop_reason="max_tokens", usage=_ANTHROPIC_USAGE,
    )

    async def create(**_):
        return response

    # A tool call: message_delta says tool_use, and the final chunk must not
    # turn it into "stop".
    events = [
        SimpleNamespace(type="message_start", message=SimpleNamespace(usage=_ANTHROPIC_USAGE)),
        SimpleNamespace(type="content_block_start", index=0, content_block=SimpleNamespace(type="tool_use", id="toolu_1", name="f")),
        SimpleNamespace(type="content_block_delta", index=0, delta=SimpleNamespace(type="input_json_delta", partial_json="{}")),
        SimpleNamespace(type="content_block_stop", index=0),
        SimpleNamespace(type="message_delta", delta=SimpleNamespace(stop_reason="tool_use"), usage=SimpleNamespace(output_tokens=5, input_tokens=None)),
        SimpleNamespace(type="message_stop"),
    ]
    inner = AnthropicProvider(api_key="k")
    inner.client = SimpleNamespace(messages=SimpleNamespace(create=create, stream=lambda **_: _FakeStream(events)))
    provider = UsageTrackingProvider(inner, provider_name="a", provider_type="anthropic")
    await provider.complete(MESSAGES, model="m")
    chunks = [c async for c in provider.stream(MESSAGES, model="m")]
    assert chunks[-1]["finish_reason"] == "tool_use"
    await _settled()
    assert [(r["finish_reason"], r["finish_reason_raw"]) for r in recorded] == [
        ("length", "max_tokens"), ("tool_calls", "tool_use"),
    ]


# ── Ollama ─────────────────────────────────────────────────────────────────


async def test_ollama_records_its_done_reason(recorded, monkeypatch):
    def handler(request):
        if json.loads(request.content).get("stream"):
            lines = [
                {"message": {"role": "assistant", "content": "Hi"}, "done": False},
                {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "length",
                 "prompt_eval_count": 3, "eval_count": 1},
            ]
            return httpx.Response(200, content="\n".join(json.dumps(line) for line in lines).encode())
        return httpx.Response(200, json={
            "message": {"role": "assistant", "content": "Hi"}, "done": True, "done_reason": "stop",
            "prompt_eval_count": 3, "eval_count": 1,
        })

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "app.providers.ollama_provider.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    provider = UsageTrackingProvider(OllamaProvider(base_url="http://ollama.test"), provider_name="o", provider_type="ollama")
    await provider.complete(MESSAGES, model="m")
    [c async for c in provider.stream(MESSAGES, model="m")]
    await _settled()
    assert [(r["finish_reason"], r["finish_reason_raw"]) for r in recorded] == [("stop", "stop"), ("length", "length")]


# ── failures ───────────────────────────────────────────────────────────────


class _Failing:
    api_key = None
    base_url = None

    async def complete(self, *a, **k):
        raise RuntimeError("boom")

    async def stream(self, *a, **k):
        # OpenAI sends the finish reason before its usage chunk: a failure
        # after it still makes the call a failed one.
        yield {"content": "par", "finish_reason": "stop"}
        raise RuntimeError("boom")


async def test_a_failed_call_is_recorded_as_error(recorded):
    provider = UsageTrackingProvider(_Failing(), provider_name="p")
    with pytest.raises(RuntimeError):
        await provider.complete(MESSAGES, model="m")
    with pytest.raises(RuntimeError):
        [c async for c in provider.stream(MESSAGES, model="m")]
    await _settled()
    assert [(r["finish_reason"], r["finish_reason_raw"]) for r in recorded] == [("error", None), ("error", "stop")]


# ── provider batches ───────────────────────────────────────────────────────


async def test_batch_results_carry_the_finish_reason():
    output = json.dumps({
        "custom_id": "exec-1",
        "response": {"status_code": 200, "body": {
            "choices": [{"message": {"content": "hello"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }},
    })
    openai_provider = OpenAIProvider(api_key="k")

    async def retrieve(batch_id):
        return SimpleNamespace(status="completed", output_file_id="out", error_file_id=None)

    async def content(file_id):
        return SimpleNamespace(text=output + "\n")

    openai_provider.client = SimpleNamespace(
        batches=SimpleNamespace(retrieve=retrieve), files=SimpleNamespace(content=content),
    )
    (item,) = await openai_provider.fetch_batch_results("b")
    assert item["finish_reason"] == "length"

    message = SimpleNamespace(content=[SimpleNamespace(type="text", text="hi")], usage=_ANTHROPIC_USAGE, stop_reason="end_turn")
    entry = SimpleNamespace(custom_id="exec-1", result=SimpleNamespace(type="succeeded", message=message))

    async def results(batch_id):
        class _Iter:
            def __aiter__(self):
                return self._gen()

            async def _gen(self):
                yield entry

        return _Iter()

    anthropic_provider = AnthropicProvider(api_key="k")
    anthropic_provider.client = SimpleNamespace(messages=SimpleNamespace(batches=SimpleNamespace(results=results)))
    (item,) = await anthropic_provider.fetch_batch_results("b")
    assert normalize_finish_reason(item["finish_reason"]) == "stop"


# ── the usage API ──────────────────────────────────────────────────────────


async def test_usage_calls_return_the_finish_reason(client, db, admin_user):
    from tests.unit.test_usage_api import _get, _row

    chat = uuid.uuid4()
    db.add_all([
        _row(user_id=admin_user.id, chat_id=chat, finish_reason="length", finish_reason_raw="max_tokens"),
        _row(user_id=admin_user.id, chat_id=chat),  # recorded before it was kept
    ])
    await db.flush()
    body = await _get(client, admin_user, "calls", {"chat_id": str(chat)})
    assert sorted(
        ((i["finish_reason"], i["finish_reason_raw"]) for i in body["items"]), key=str
    ) == sorted([("length", "max_tokens"), (None, None)], key=str)


def test_the_migration_leaves_one_head():
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend = Path(__file__).resolve().parents[2]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    # One head (a later migration may follow this one), and this one in its
    # history.
    assert len(heads) == 1, heads
    script = ScriptDirectory.from_config(config)
    assert "a9f1n2s3h4r5" in {rev.revision for rev in script.walk_revisions()}
