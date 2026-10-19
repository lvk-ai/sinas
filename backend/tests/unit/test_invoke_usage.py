"""`usage` on the synchronous agent invoke (and non-streaming message send).

The LLM is scripted per chat; everything else is the real stack: the
invoke endpoint, MessageService with its tool rounds, the usage-tracking
wrapper and the real llm_usage insert and pricing. Data is committed (not
rolled back per test) so concurrent requests run on their own sessions, as
in production; each test uses fresh agents, users and chats.
"""

import asyncio
import json
import uuid
from decimal import Decimal
from typing import Any, Optional

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal, async_engine
from app.models import LLMUsage
from app.models.agent import Agent
from app.models.chat import Chat, Message
from app.models.user import Role, RolePermission, User, UserRole
from app.providers import tracking
from app.providers.base import BaseLLMProvider
from app.providers.tracking import UsageTrackingProvider, collect_usage
from app.services import llm_pricing
from app.services import message_service as ms
from tests.conftest import auth_headers

PRICED_MODEL = "invoke-usage-priced"
UNPRICED_MODEL = "invoke-usage-unpriced"
# $1 per million input tokens, $2 per million output tokens
RATECARD = {PRICED_MODEL: {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6}}


def _usage(prompt: int, completion: int, cache_read: int = 0, cache_write: int = 0) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }


def _text(content: str, usage: dict, delay: float = 0.0) -> dict:
    return {"content": content, "tool_calls": None, "usage": usage, "delay": delay}


def _tool_call(name: str, usage: dict, delay: float = 0.0) -> dict:
    call = {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }
    return {"content": None, "tool_calls": [call], "usage": usage, "delay": delay}


class ScriptedLLM(BaseLLMProvider):
    """Plays the model: pops the next scripted step of the chat it serves,
    for both the first (complete) call and the follow-ups (stream)."""

    scripts: dict[str, list[dict]] = {}

    def __init__(self, chat_id: str):
        super().__init__(api_key="k")
        self._chat_id = chat_id

    def _next(self) -> dict:
        return self.scripts[self._chat_id].pop(0)

    async def complete(self, messages, model, tools=None, temperature=0.7, max_tokens=None, **kw):
        step = self._next()
        if step["delay"]:
            await asyncio.sleep(step["delay"])
        return {
            "content": step["content"],
            "tool_calls": step["tool_calls"],
            "usage": step["usage"],
            "finish_reason": "tool_calls" if step["tool_calls"] else "stop",
        }

    async def stream(self, messages, model, tools=None, temperature=0.7, max_tokens=None, **kw):
        step = self._next()
        if step["delay"]:
            await asyncio.sleep(step["delay"])
        deltas = None
        if step["tool_calls"]:
            deltas = [{"index": i, **tc} for i, tc in enumerate(step["tool_calls"])]
        yield {"content": step["content"], "tool_calls": deltas, "finish_reason": None}
        yield {"content": None, "tool_calls": None, "usage": step["usage"], "finish_reason": "stop"}

    def format_tool_calls(self, tool_calls: Any) -> list[dict[str, Any]]:
        return tool_calls or []

    def extract_usage(self, response: Any) -> dict[str, int]:
        return {}


@pytest_asyncio.fixture(autouse=True)
async def _scripted_llm(monkeypatch):
    """Every chat-pipeline provider is the scripted model wrapped in the real
    usage tracking; the deployment ratecard prices PRICED_MODEL only."""

    async def fake_create_provider(provider_name, model, db, usage_context=None, **kw):
        chat_id = str((usage_context or {}).get("chat_id"))
        return UsageTrackingProvider(
            ScriptedLLM(chat_id),
            provider_name=None,
            provider_type="openai",
            context=usage_context,
        )

    monkeypatch.setattr(ms, "create_provider", fake_create_provider)
    monkeypatch.setattr(settings, "model_prices_json", json.dumps(RATECARD))
    monkeypatch.setattr(settings, "model_prices_file", None)
    monkeypatch.setattr(settings, "model_prices_url", None)
    monkeypatch.setattr(settings, "compaction_enabled", False)
    llm_pricing.reset_cache()
    ScriptedLLM.scripts = {}
    yield
    llm_pricing.reset_cache()
    await async_engine.dispose()


@pytest_asyncio.fixture
async def env():
    """A committed user allowed to chat with any agent, and a factory for
    committed agents. Removed afterwards (with their chats and usage)."""
    created_agents: list[uuid.UUID] = []
    async with AsyncSessionLocal() as s:
        role = Role(name=f"invoke-usage-{uuid.uuid4().hex[:8]}", description="test")
        s.add(role)
        await s.flush()
        s.add(
            RolePermission(
                role_id=role.id, permission_key="sinas.agents/*/*.chat:all", permission_value=True
            )
        )
        user = User(email=f"invoke-usage-{uuid.uuid4().hex[:8]}@example.com")
        s.add(user)
        await s.flush()
        s.add(UserRole(role_id=role.id, user_id=user.id, active=True))
        await s.commit()
        await s.refresh(user)

    async def make_agent(model: str = PRICED_MODEL) -> Agent:
        async with AsyncSessionLocal() as s:
            agent = Agent(
                namespace="invoke-usage",
                name=f"a-{uuid.uuid4().hex[:10]}",
                model=model,
                system_prompt="You are a test agent.",
                is_active=True,
            )
            s.add(agent)
            await s.commit()
            await s.refresh(agent)
        created_agents.append(agent.id)
        return agent

    yield {"user": user, "make_agent": make_agent}

    async with AsyncSessionLocal() as s:
        chat_ids = (await s.execute(select(Chat.id).where(Chat.user_id == user.id))).scalars().all()
        if chat_ids:
            await s.execute(delete(LLMUsage).where(LLMUsage.chat_id.in_(chat_ids)))
            await s.execute(delete(Message).where(Message.chat_id.in_(chat_ids)))
            await s.execute(delete(Chat).where(Chat.id.in_(chat_ids)))
        if created_agents:
            await s.execute(delete(Agent).where(Agent.id.in_(created_agents)))
        await s.execute(delete(UserRole).where(UserRole.user_id == user.id))
        await s.execute(delete(User).where(User.id == user.id))
        await s.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
        await s.execute(delete(Role).where(Role.id == role.id))
        await s.commit()


@pytest_asyncio.fixture
async def http():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _invoke(http, user, agent, body: dict) -> dict:
    resp = await http.post(
        f"/agents/{agent.namespace}/{agent.name}/invoke", json=body, headers=auth_headers(user)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _rows(chat_id: str) -> list[LLMUsage]:
    async with AsyncSessionLocal() as s:
        return list(
            (await s.execute(select(LLMUsage).where(LLMUsage.chat_id == uuid.UUID(chat_id))))
            .scalars()
            .all()
        )


async def _new_session_chat(user: User, agent: Agent, session_key: str) -> str:
    """The chat an invoke with this session_key will use — created the way the
    endpoint creates it, so the scripted steps can be keyed by its id."""
    async with AsyncSessionLocal() as s:
        chat = Chat(
            user_id=user.id,
            agent_id=agent.id,
            agent_namespace=agent.namespace,
            agent_name=agent.name,
            title=f"invoke:{session_key}",
            session_key=session_key,
        )
        s.add(chat)
        await s.commit()
        return str(chat.id)


def _script(chat_id: str, *steps: dict) -> None:
    ScriptedLLM.scripts[chat_id] = list(steps)


# ---------------------------------------------------------------------------
# The invoke endpoint
# ---------------------------------------------------------------------------


async def test_single_call(env, http):
    agent = await env["make_agent"]()
    chat_id = await _new_session_chat(env["user"], agent, "single")
    _script(chat_id, _text("hello", _usage(1000, 100)))

    out = await _invoke(http, env["user"], agent, {"message": "hi", "session_key": "single"})

    assert out["reply"] == "hello" and out["chat_id"] == chat_id
    assert out["usage"] == {
        "calls": 1,
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "total_tokens": 1100,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": pytest.approx(0.0012),  # 1000 * 1e-6 + 100 * 2e-6
        "unpriced_calls": 0,
        "errors": 0,
    }
    # The rows are committed by the time the response is returned.
    rows = await _rows(chat_id)
    assert len(rows) == 1 and rows[0].cost_usd == Decimal("0.0012")


async def test_one_shot_invoke_without_session_key(env, http, monkeypatch):
    """A one-shot invoke creates its chat inside the request; script by the
    first chat the scripted model sees."""
    agent = await env["make_agent"]()

    class _AnyChat(dict):
        def __missing__(self, key):
            steps = [_text("one-shot", _usage(10, 5))]
            self[key] = steps
            return steps

    ScriptedLLM.scripts = _AnyChat()
    out = await _invoke(http, env["user"], agent, {"message": "hi"})
    assert out["usage"]["calls"] == 1
    assert out["usage"]["total_tokens"] == 15
    assert len(await _rows(out["chat_id"])) == 1


async def test_tool_rounds_are_counted(env, http):
    agent = await env["make_agent"]()
    chat_id = await _new_session_chat(env["user"], agent, "tools")
    _script(
        chat_id,
        _tool_call("lookup_order", _usage(500, 20)),  # complete()
        _tool_call("lookup_order", _usage(600, 30)),  # follow-up stream, another round
        _text("your order shipped", _usage(700, 40)),  # final follow-up
    )

    out = await _invoke(
        http, env["user"], agent, {"message": "where is my order?", "session_key": "tools"}
    )

    assert out["reply"] == "your order shipped"
    usage = out["usage"]
    assert usage["calls"] == 3
    assert (usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"]) == (
        1800,
        90,
        1890,
    )
    assert usage["cost_usd"] == pytest.approx(1800e-6 + 90 * 2e-6)
    assert usage["unpriced_calls"] == 0
    assert ScriptedLLM.scripts[chat_id] == []  # every scripted round ran
    rows = await _rows(chat_id)
    assert len(rows) == 3 and sum(r.total_tokens for r in rows) == 1890


async def test_cache_tokens(env, http):
    agent = await env["make_agent"]()
    chat_id = await _new_session_chat(env["user"], agent, "cache")
    _script(
        chat_id,
        _tool_call("lookup_order", _usage(2000, 10, cache_write=1500)),
        _text("done", _usage(2100, 20, cache_read=1500)),
    )

    usage = (await _invoke(http, env["user"], agent, {"message": "hi", "session_key": "cache"}))[
        "usage"
    ]

    assert (usage["cache_read_tokens"], usage["cache_write_tokens"]) == (1500, 1500)
    assert usage["prompt_tokens"] == 4100 and usage["calls"] == 2


async def test_row_that_fails_to_commit_is_reported_unpriced(env, http, monkeypatch):
    """The real insert prices the row, then its commit fails in Postgres: the
    response must not claim a cost /usage has no row for."""
    real_price_usage = llm_pricing.price_usage

    async def price_then_break(session, values):
        await real_price_usage(session, values)
        assert values.get("cost_usd") is not None  # it was priced
        values["provider_type"] = "x" * 100  # longer than the column: commit fails

    monkeypatch.setattr(llm_pricing, "price_usage", price_then_break)
    agent = await env["make_agent"]()
    chat_id = await _new_session_chat(env["user"], agent, "unsaved")
    _script(chat_id, _text("hello", _usage(1000, 100)))

    usage = (await _invoke(http, env["user"], agent, {"message": "hi", "session_key": "unsaved"}))[
        "usage"
    ]

    assert await _rows(chat_id) == []  # nothing saved
    # The call happened and is counted, but without a cost.
    assert (usage["calls"], usage["total_tokens"]) == (1, 1100)
    assert usage["cost_usd"] is None and usage["unpriced_calls"] == 1


async def test_unpriced_model_has_null_cost(env, http):
    agent = await env["make_agent"](UNPRICED_MODEL)
    chat_id = await _new_session_chat(env["user"], agent, "unpriced")
    _script(chat_id, _tool_call("lookup_order", _usage(100, 10)), _text("ok", _usage(120, 12)))

    usage = (await _invoke(http, env["user"], agent, {"message": "hi", "session_key": "unpriced"}))[
        "usage"
    ]

    assert usage["calls"] == 2 and usage["total_tokens"] == 242
    assert usage["cost_usd"] is None
    assert usage["unpriced_calls"] == 2


async def test_session_second_invoke_counts_only_itself(env, http):
    agent = await env["make_agent"]()
    chat_id = await _new_session_chat(env["user"], agent, "sess")

    _script(chat_id, _tool_call("lookup_order", _usage(100, 10)), _text("first", _usage(110, 11)))
    first = await _invoke(http, env["user"], agent, {"message": "one", "session_key": "sess"})
    _script(chat_id, _text("second", _usage(300, 30)))
    second = await _invoke(http, env["user"], agent, {"message": "two", "session_key": "sess"})

    assert first["chat_id"] == second["chat_id"] == chat_id
    assert first["usage"]["calls"] == 2 and first["usage"]["total_tokens"] == 231
    assert second["usage"]["calls"] == 1 and second["usage"]["total_tokens"] == 330
    assert second["usage"]["cost_usd"] == pytest.approx(300e-6 + 30 * 2e-6)
    # The chat as a whole has both invokes' calls — what per-chat totals would report.
    assert len(await _rows(chat_id)) == 3


async def test_concurrent_invokes_on_different_chats_dont_mix(env, http):
    agent_a = await env["make_agent"]()
    agent_b = await env["make_agent"]()
    chat_a = await _new_session_chat(env["user"], agent_a, "conc")
    chat_b = await _new_session_chat(env["user"], agent_b, "conc")
    # Interleaved: B's rounds land while A's are in flight, and vice versa.
    _script(
        chat_a,
        _tool_call("lookup_order", _usage(1000, 1), delay=0.05),
        _text("a", _usage(1000, 1), delay=0.15),
    )
    _script(
        chat_b,
        _tool_call("lookup_order", _usage(7, 3), delay=0.1),
        _tool_call("lookup_order", _usage(7, 3), delay=0.02),
        _text("b", _usage(7, 3), delay=0.02),
    )

    out_a, out_b = await asyncio.gather(
        _invoke(http, env["user"], agent_a, {"message": "a", "session_key": "conc"}),
        _invoke(http, env["user"], agent_b, {"message": "b", "session_key": "conc"}),
    )

    assert (out_a["reply"], out_b["reply"]) == ("a", "b")
    assert (out_a["usage"]["calls"], out_a["usage"]["total_tokens"]) == (2, 2002)
    assert (out_b["usage"]["calls"], out_b["usage"]["total_tokens"]) == (3, 30)


async def test_message_send_returns_usage_too(env, http):
    agent = await env["make_agent"]()
    chat_id = await _new_session_chat(env["user"], agent, "send")
    _script(chat_id, _text("first", _usage(50, 5)))
    await _invoke(http, env["user"], agent, {"message": "one", "session_key": "send"})
    _script(chat_id, _tool_call("lookup_order", _usage(60, 6)), _text("reply", _usage(70, 7)))

    resp = await http.post(
        f"/chats/{chat_id}/messages", json={"content": "two"}, headers=auth_headers(env["user"])
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content"] == "reply" and body["role"] == "assistant"
    assert body["usage"]["calls"] == 2 and body["usage"]["total_tokens"] == 143


def test_invoke_usage_is_typed_in_openapi():
    from app.main import app

    schemas = app.openapi()["components"]["schemas"]
    usage_ref = schemas["InvokeResponse"]["properties"]["usage"]
    assert any(
        option.get("$ref", "").endswith("/TurnUsage")
        for option in usage_ref.get("anyOf", [usage_ref])
    )
    assert set(schemas["TurnUsage"]["properties"]) == {
        "calls",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_usd",
        "unpriced_calls",
        "errors",
    }


# ---------------------------------------------------------------------------
# The collector itself
# ---------------------------------------------------------------------------


class _Fixed(BaseLLMProvider):
    def __init__(self, usage: dict, delay: float = 0.0):
        super().__init__(api_key="k")
        self._usage, self._delay = usage, delay

    async def complete(self, messages, model, tools=None, temperature=0.7, max_tokens=None, **kw):
        await asyncio.sleep(self._delay)
        return {"content": "x", "tool_calls": None, "usage": self._usage}

    async def stream(self, *a, **kw):  # pragma: no cover - unused
        yield {}

    def format_tool_calls(self, tool_calls):
        return []

    def extract_usage(self, response):
        return {}


def _fake_insert(
    monkeypatch, price: Optional[Decimal] = None, delay: float = 0.0, saved: bool = True
):
    async def insert(values):
        await asyncio.sleep(delay)
        if price is not None:
            values["cost_usd"] = price
        return saved

    monkeypatch.setattr(tracking, "_insert_usage", insert)


async def test_collector_counts_spawned_tasks_but_not_detached_or_late_ones(monkeypatch):
    _fake_insert(monkeypatch, price=Decimal("0.5"))
    llm = UsageTrackingProvider(_Fixed(_usage(10, 1)))

    async def detached():
        tracking.detach_usage_collector()
        await llm.complete(messages=[], model="m")

    with collect_usage() as collector:
        await llm.complete(messages=[], model="m")
        await asyncio.gather(llm.complete(messages=[], model="m"), asyncio.create_task(detached()))
        late = asyncio.create_task(llm.complete(messages=[], model="m"))  # records after totals
        totals = await collector.totals()
    await late

    assert totals["calls"] == 2 and totals["total_tokens"] == 22
    assert totals["cost_usd"] == pytest.approx(1.0)


async def test_collector_waits_for_inserts_and_flags_unfinished_ones(monkeypatch):
    llm = UsageTrackingProvider(_Fixed(_usage(10, 1)))

    _fake_insert(monkeypatch, price=Decimal("0.25"), delay=0.05)
    with collect_usage() as collector:
        await llm.complete(messages=[], model="m")
    totals = await collector.totals()
    assert totals["cost_usd"] == pytest.approx(0.25) and totals["unpriced_calls"] == 0

    _fake_insert(monkeypatch, price=Decimal("0.25"), delay=0.5)
    with collect_usage() as collector:
        await llm.complete(messages=[], model="m")
    totals = await collector.totals(timeout=0.05)
    # Not committed in time: counted, but not silently priced.
    assert totals["calls"] == 1 and totals["cost_usd"] is None and totals["unpriced_calls"] == 1
    await asyncio.sleep(0.5)  # let the slow insert finish inside this test's loop


async def test_collector_excludes_the_cost_of_rows_that_failed_to_save(monkeypatch):
    llm = UsageTrackingProvider(_Fixed(_usage(10, 1)))
    # Priced, then the commit failed: the cost must not be reported.
    _fake_insert(monkeypatch, price=Decimal("0.25"), saved=False)
    with collect_usage() as collector:
        await llm.complete(messages=[], model="m")
    totals = await collector.totals()
    assert totals["calls"] == 1 and totals["total_tokens"] == 11
    assert totals["cost_usd"] is None and totals["unpriced_calls"] == 1


async def test_collector_without_calls():
    with collect_usage() as collector:
        pass
    totals = await collector.totals()
    assert totals["calls"] == 0 and totals["cost_usd"] == 0.0 and totals["unpriced_calls"] == 0
