"""Loop-controls review follow-ups: injected messages never split a tool
exchange and never go unanswered; continuations that find the chat busy
retry instead of giving up; an interrupt stops continuations queued before
it; a failed session grant does not strand an approved call; approval
rules are validated at save time."""
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import async_engine
from app.models.chat import Chat, Message
from app.models.user import User
from app.services import approval_rules, chat_steering
from app.services.conversation_history import build_conversation_history
from app.services.message_service import MessageService
from app.services.queue_service import queue_service
from app.services.skill_tools import SkillToolConverter
from tests.conftest import auth_headers
from tests.unit.test_tool_approval_resolution import _capture, _pending


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine_per_test():
    yield
    await async_engine.dispose()


@pytest_asyncio.fixture
async def chat(db: AsyncSession, test_user: User) -> Chat:
    c = Chat(user_id=test_user.id, title="review chat")
    db.add(c)
    await db.flush()
    await db.refresh(c)
    return c


async def _add(db, chat, role, content=None, tool_calls=None, tool_call_id=None, name=None):
    m = Message(chat_id=chat.id, role=role, content=content, tool_calls=tool_calls, tool_call_id=tool_call_id, name=name)
    db.add(m)
    await db.flush()
    return m


def _tc(cid):
    return {"id": cid, "type": "function", "function": {"name": "t", "arguments": "{}"}}


# ---------------------------------------------------------------------------
# Steering primitives
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_injection_counter_and_interrupt_timestamp():
    cid = str(uuid.uuid4())
    assert await chat_steering.take_injections(cid) == 0
    await chat_steering.note_injection(cid)
    await chat_steering.note_injection(cid)
    assert await chat_steering.take_injections(cid) == 2
    assert await chat_steering.take_injections(cid) == 0  # taken

    assert await chat_steering.interrupt_requested_at(cid) is None
    before = time.time()
    await chat_steering.request_interrupt(cid, "op")
    at = await chat_steering.interrupt_requested_at(cid)
    assert at is not None and at >= before - 1
    assert await chat_steering.consume_interrupt(cid)
    assert await chat_steering.interrupt_requested_at(cid) is not None  # survives the flag
    await chat_steering.clear_interrupt(cid)
    assert await chat_steering.interrupt_requested_at(cid) is None


# ---------------------------------------------------------------------------
# History: injected messages stay out of tool exchanges
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_injected_message_moves_after_the_tool_results(db, chat, monkeypatch):
    monkeypatch.setattr(settings, "max_history_messages", 100)
    await _add(db, chat, "user", "do it")
    await _add(db, chat, "assistant", None, tool_calls=[_tc("c1"), _tc("c2")])
    await _add(db, chat, "user", "injected mid-round")
    await _add(db, chat, "tool", "{}", tool_call_id="c1", name="t")
    await _add(db, chat, "tool", "{}", tool_call_id="c2", name="t")
    await _add(db, chat, "assistant", "done")
    sent = await build_conversation_history(db, chat, SkillToolConverter())
    assert [(m["role"], m.get("tool_call_id")) for m in sent] == [
        ("user", None), ("assistant", None), ("tool", "c1"), ("tool", "c2"), ("user", None), ("assistant", None),
    ]
    assert sent[4]["content"] == "injected mid-round"


@pytest.mark.asyncio
async def test_injected_message_with_missing_results_lands_after_the_repair(db, chat, monkeypatch):
    monkeypatch.setattr(settings, "max_history_messages", 100)
    await _add(db, chat, "assistant", None, tool_calls=[_tc("c1")])
    await _add(db, chat, "user", "injected")
    await _add(db, chat, "assistant", "gave up")
    sent = await build_conversation_history(db, chat, SkillToolConverter())
    assert [m["role"] for m in sent] == ["assistant", "tool", "user", "assistant"]
    assert sent[1]["tool_call_id"] == "c1"  # synthetic result directly behind the call


# ---------------------------------------------------------------------------
# Injections answered at the end of a text-only turn
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pending_injections_get_a_follow_up(db, chat, test_user, monkeypatch):
    from app.services import message_service as ms

    seen = []

    class _P:
        async def stream(self, messages, model, tools, temperature, max_tokens):
            seen.append([m.get("content") for m in messages if m["role"] == "user"])
            yield {"content": "answering the injection", "tool_calls": None, "finish_reason": "stop"}

    async def fake_provider(*_a, **_k):
        return _P()

    monkeypatch.setattr(ms, "create_provider", fake_provider)
    svc = MessageService(db)
    await _add(db, chat, "user", "first")
    await _add(db, chat, "assistant", "final text reply")
    # Nothing injected: nothing happens.
    out = [c async for c in svc.answer_pending_injections(
        chat_id=str(chat.id), user_id=str(test_user.id), user_token="", provider=None, model=None,
        temperature=0.7, max_tokens=None, tools=[],
    )]
    assert out == [] and seen == []

    injected = await svc.inject_user_message(str(chat.id), str(test_user.id), "while you were typing")
    assert injected is not None
    out = [c async for c in svc.answer_pending_injections(
        chat_id=str(chat.id), user_id=str(test_user.id), user_token="", provider=None, model=None,
        temperature=0.7, max_tokens=None, tools=[],
    )]
    assert out[0] == {"type": "injection_followup"}
    assert seen == [["first", "while you were typing"]]
    rows = (await db.execute(select(Message).where(Message.chat_id == chat.id).order_by(Message.created_at))).scalars().all()
    assert [r.role for r in rows][-2:] == ["user", "assistant"]
    assert rows[-1].content == "answering the injection"
    assert await chat_steering.take_injections(str(chat.id)) == 0


# ---------------------------------------------------------------------------
# Continuations: busy lock retries, interrupt ordering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_busy_continuation_is_requeued_with_backoff(monkeypatch):
    from app.queue import agent_jobs
    from app.services.stream_relay import stream_relay

    published = []

    async def fake_publish(channel_id, event, ttl=None):
        published.append(event)

    async def fake_error(channel_id, msg):
        published.append({"type": "error", "error": msg})

    monkeypatch.setattr(stream_relay, "publish", fake_publish)
    monkeypatch.setattr(stream_relay, "publish_error", fake_error)
    attempts = []

    async def enqueue(attempt):
        attempts.append(attempt)

    assert await agent_jobs._requeue_when_busy({"attempt": 0}, "chan", enqueue) is True
    assert attempts == [1] and published[-1]["status"] == "waiting"
    assert await agent_jobs._requeue_when_busy({"attempt": agent_jobs._BUSY_MAX_ATTEMPTS}, "chan", enqueue) is False
    assert attempts == [1] and published[-1]["type"] == "error"


@pytest.mark.asyncio
async def test_continuation_queued_before_an_interrupt_stops(db, chat, monkeypatch):
    from app.queue import agent_jobs
    from app.services.stream_relay import stream_relay

    published = []

    async def fake_publish(channel_id, event, ttl=None):
        published.append(event)

    async def fake_done(channel_id):
        published.append({"type": "done"})

    monkeypatch.setattr(stream_relay, "publish", fake_publish)
    monkeypatch.setattr(stream_relay, "publish_done", fake_done)
    cid = str(chat.id)
    queued_at = time.time()
    await chat_steering.request_interrupt(cid, "op")
    # The running turn consumed the flag already...
    assert await chat_steering.consume_interrupt(cid)
    # ...but a continuation queued before the request still stops.
    assert await agent_jobs._stopped_by_interrupt(cid, {"enqueued_at": queued_at - 1}, "chan", db) is True
    assert [p["type"] for p in published] == ["interrupted", "done"]
    rows = (await db.execute(select(Message).where(Message.chat_id == chat.id))).scalars().all()
    assert rows[-1].role == "system" and rows[-1].content == chat_steering.INTERRUPT_MARKER
    # One queued after it runs.
    assert await agent_jobs._stopped_by_interrupt(cid, {"enqueued_at": time.time() + 1}, "chan", db) is False
    await chat_steering.clear_interrupt(cid)


@pytest.mark.asyncio
async def test_resume_enqueue_carries_retry_fields(monkeypatch):
    from app.core import redis as redis_mod

    jobs = []

    class FakePool:
        async def enqueue_job(self, name, **kwargs):
            jobs.append((name, kwargs))

    class FakeRedis:
        async def set(self, *a, **k):
            return True

    async def fake_pool():
        return FakePool()

    async def fake_redis():
        return FakeRedis()

    import app.services.queue_service as qs
    monkeypatch.setattr(qs, "get_arq_pool", fake_pool)
    monkeypatch.setattr(qs, "get_redis", fake_redis)
    await queue_service.enqueue_agent_resume(
        chat_id="c", user_id="u", user_token="t", pending_approval_id="p", approved=True,
        channel_id="ch", defer_seconds=30, attempt=2, enqueued_at=123.0,
    )
    name, kw = jobs[-1]
    assert name == "execute_agent_resume_job" and kw["attempt"] == 2 and kw["enqueued_at"] == 123.0
    assert kw["_defer_by"].total_seconds() == 30
    await queue_service.enqueue_agent_resume(
        chat_id="c", user_id="u", user_token="t", pending_approval_id="p", approved=True, channel_id="ch",
    )
    assert "_defer_by" not in jobs[-1][1] and jobs[-1][1]["attempt"] == 0 and jobs[-1][1]["enqueued_at"] > 0


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failed_session_grant_does_not_strand_the_approval(client, db, admin_user, monkeypatch):
    calls = _capture(monkeypatch)

    async def boom(chat_id, tool_name):
        raise RuntimeError("redis down")

    monkeypatch.setattr(approval_rules, "add_session_grant", boom)
    chat_id, call_id = await _pending(db, admin_user, datetime.now(UTC) + timedelta(hours=1))
    r = await client.post(
        f"/chats/{chat_id}/approve-tool/{call_id}", json={"approved": True, "always_allow": True},
        headers=auth_headers(admin_user),
    )
    assert r.status_code == 202, r.text
    assert len(calls) == 1 and calls[0]["approved"] is True


def test_tool_approvals_are_validated_at_save_time():
    from pydantic import ValidationError

    from app.schemas.agent import AgentCreate
    from app.schemas.config import AgentConfig

    v = approval_rules.validate_tool_approvals
    assert v(None) is None
    assert v({"rules": [{"match": "write_*", "action": "ask"}]}) == {"default": "auto", "rules": [{"match": "write_*", "action": "ask"}]}
    for bad in ({"rules": ["write_*"]}, {"rules": [{"match": 42, "action": "ask"}]}, {"default": "maybe"},
                {"rules": [{"match": "x", "action": "later"}]}, {"rulez": []}, "ask", {"rules": {"match": "x"}}):
        with pytest.raises(ValueError):
            v(bad)
    with pytest.raises(ValidationError):
        AgentCreate(namespace="n", name="a", tool_approvals={"rules": ["write_*"]})
    with pytest.raises(ValidationError):
        AgentConfig(namespace="n", name="a", toolApprovals={"rules": [{"match": 42, "action": "ask"}]})
    ok = AgentCreate(namespace="n", name="a", tool_approvals={"rules": [{"match": "x", "action": "ask"}]})
    assert ok.tool_approvals["default"] == "auto"


def test_resolver_survives_whatever_is_stored():
    r = approval_rules.resolve_action
    assert r({"rules": ["write_*"]}, "write_x", False, set()) == "auto"
    assert r({"rules": [{"match": 42, "action": "ask"}, {"match": "write_*", "action": "ask"}]}, "write_x", False, set()) == "ask"
    assert r({"rules": {"match": "x"}}, "x", True, set()) == "ask"
    assert r("garbage", "x", False, set()) == "auto"


def test_tool_approvals_travel_through_the_agent_spec():
    """Config apply and the REST API both go through AgentSpec now: the
    rules are validated there and survive the row round trip."""
    from pydantic import ValidationError

    from app.models.agent import Agent
    from app.schemas.spec.agent import AgentSpec
    from app.services.resources.agents import AgentApplier

    spec = AgentSpec(namespace="n", name="a", toolApprovals={"rules": [{"match": "write_*", "action": "ask"}]})
    assert spec.tool_approvals == {"default": "auto", "rules": [{"match": "write_*", "action": "ask"}]}
    assert spec.to_config()["toolApprovals"]["rules"][0]["match"] == "write_*"
    with pytest.raises(ValidationError):
        AgentSpec(namespace="n", name="a", toolApprovals={"rules": ["write_*"]})

    row = Agent(namespace="n", name="a", tool_approvals={"default": "ask", "rules": []})
    assert AgentApplier().spec_from_row(row, None).tool_approvals == {"default": "ask", "rules": []}
    row2 = Agent(namespace="n", name="b")
    assert AgentApplier().spec_from_row(row2, None).tool_approvals is None
