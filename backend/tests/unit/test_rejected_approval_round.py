"""A round the user rejected in full still answers every call.

An assistant message with tool_calls must be followed by a result for each
call, or the provider rejects the next request; and the docs promise that a
rejected call gets "a rejection result the model can react to". The mixed
case (some approved) always did that through `_handle_tool_calls`; a round
with nothing approved used to only publish `tool_rejected` events and stop,
leaving the calls unanswered in the transcript and the model silent.

Runs the real resume job against Postgres + Redis (the job opens its own
sessions, so rows are committed and cleaned up), with the model follow-up
and tool execution stubbed.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal, async_engine
from app.models.chat import Chat, Message
from app.models.pending_approval import PendingToolApproval
from app.models.user import User
from app.queue.agent_jobs import execute_agent_resume_job
from app.services import chat_steering
from app.services import deferred_completions as dc
from app.services import message_service as ms
from app.services.queue_service import queue_service
from app.services.stream_relay import stream_relay


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine_per_test():
    yield
    await async_engine.dispose()


@pytest_asyncio.fixture
async def env():
    async with AsyncSessionLocal() as s:
        user = User(email=f"rejround-{uuid.uuid4().hex[:8]}@example.com")
        s.add(user)
        await s.flush()
        chat = Chat(user_id=user.id, title="rejected round test")
        s.add(chat)
        await s.commit()
        env = {"user_id": str(user.id), "chat_id": str(chat.id)}

    yield env

    async with AsyncSessionLocal() as s:
        await s.execute(delete(PendingToolApproval).where(PendingToolApproval.chat_id == env["chat_id"]))
        await s.execute(delete(Message).where(Message.chat_id == env["chat_id"]))
        await s.execute(delete(Chat).where(Chat.id == env["chat_id"]))
        await s.execute(delete(User).where(User.id == env["user_id"]))
        await s.commit()
    await chat_steering.clear_interrupt(env["chat_id"])


@pytest.fixture
def harness(monkeypatch):
    """Capture stream events, tool executions and follow-up model turns."""
    seen = {"events": [], "ran": [], "followups": 0}

    async def publish(channel_id, event, ttl=None):
        seen["events"].append(event)

    async def publish_done(channel_id, *args, **kwargs):
        seen["events"].append({"type": "done"})

    async def publish_error(channel_id, error, *args, **kwargs):
        seen["events"].append({"type": "error", "error": error})

    async def fake_execute(tc, *args, **kwargs):
        seen["ran"].append(tc["id"])
        return (tc["id"], tc["function"]["name"], json.dumps({"ok": True}))

    async def followup(self, **kwargs):
        seen["followups"] += 1
        yield {"type": "message", "content": "understood"}

    monkeypatch.setattr(stream_relay, "publish", publish)
    monkeypatch.setattr(stream_relay, "publish_done", publish_done)
    monkeypatch.setattr(stream_relay, "publish_error", publish_error)
    monkeypatch.setattr(ms, "execute_single_tool", fake_execute)
    monkeypatch.setattr(ms.MessageService, "_stream_followup_after_tools", followup)
    return seen


async def _round(env, asked: list[str], also: list[str] = (), approved=None, expires_at=None):
    """An assistant message whose round has `asked` calls waiting for
    approval plus `also` calls that needed none."""
    calls = [
        {"id": cid, "type": "function", "function": {"name": f"tool_{cid}", "arguments": "{}"}}
        for cid in [*asked, *also]
    ]
    async with AsyncSessionLocal() as s:
        anchor = Message(chat_id=env["chat_id"], role="assistant", content=None, tool_calls=calls)
        s.add(anchor)
        await s.flush()
        rows = []
        for cid in asked:
            row = PendingToolApproval(
                chat_id=env["chat_id"], message_id=anchor.id, user_id=env["user_id"], tool_call_id=cid,
                function_namespace="ops", function_name=f"tool_{cid}", arguments={}, all_tool_calls=calls,
                conversation_context={"messages": [], "provider": None, "model": None, "tools": []},
                expires_at=expires_at or datetime.now(timezone.utc) + timedelta(hours=1), approved=approved,
            )
            s.add(row)
            rows.append(row)
        await s.commit()
        return [str(r.id) for r in rows], [str(r.message_id) for r in rows]


async def _resume(env, pending_approval_id, approved, rejected):
    await execute_agent_resume_job(
        {},
        job_id=f"test-{uuid.uuid4().hex[:8]}",
        chat_id=env["chat_id"],
        user_id=env["user_id"],
        user_token="",
        pending_approval_id=pending_approval_id,
        approved=approved,
        rejected_tool_call_ids=rejected,
        channel_id=f"chan-{uuid.uuid4().hex[:8]}",
    )


async def _tool_results(env) -> dict[str, str]:
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                select(Message).where(Message.chat_id == env["chat_id"], Message.role == "tool")
            )
        ).scalars().all()
    return {m.tool_call_id: m.content for m in rows}


@pytest.mark.asyncio
async def test_a_partly_rejected_round_answers_the_rejected_call(env, harness):
    """The baseline the all-rejected case should match."""
    a, b = f"c-{uuid.uuid4().hex[:6]}", f"c-{uuid.uuid4().hex[:6]}"
    ids, _ = await _round(env, [a, b], approved=True)
    await _resume(env, ids[0], approved=True, rejected=[b])

    results = await _tool_results(env)
    assert set(results) == {a, b}
    assert "Rejected by user" in results[b]
    assert harness["ran"] == [a] and harness["followups"] == 1


@pytest.mark.asyncio
async def test_a_fully_rejected_round_answers_every_call_and_lets_the_model_react(env, harness):
    a, b = f"c-{uuid.uuid4().hex[:6]}", f"c-{uuid.uuid4().hex[:6]}"
    ids, _ = await _round(env, [a, b], approved=False)
    await _resume(env, ids[0], approved=False, rejected=[a, b])

    results = await _tool_results(env)
    assert set(results) == {a, b}
    assert all("Rejected by user" in content for content in results.values())
    assert harness["ran"] == []  # nothing the user refused executes
    assert harness["followups"] == 1  # the model gets to react
    rejected = {e["tool_call_id"] for e in harness["events"] if e.get("type") == "tool_rejected"}
    assert rejected == {a, b}
    assert harness["events"][-1] == {"type": "done"}


@pytest.mark.asyncio
async def test_a_fully_rejected_round_still_runs_the_calls_that_needed_no_approval(env, harness):
    """A round mixes asked and unasked calls; refusing every asked one must
    not silently drop the others (they were never up for decision)."""
    asked, free = f"c-{uuid.uuid4().hex[:6]}", f"c-{uuid.uuid4().hex[:6]}"
    ids, _ = await _round(env, [asked], also=[free], approved=False)
    await _resume(env, ids[0], approved=False, rejected=[asked])

    results = await _tool_results(env)
    assert "Rejected by user" in results[asked] and "ok" in results[free]
    assert harness["ran"] == [free]


@pytest.mark.asyncio
async def test_a_legacy_rejection_without_ids_never_runs_the_asked_call(env, harness):
    """A job queued without rejected_tool_call_ids (before rounds) and
    approved=False: the asked calls of the round are the rejected ones."""
    a, b = f"c-{uuid.uuid4().hex[:6]}", f"c-{uuid.uuid4().hex[:6]}"
    ids, _ = await _round(env, [a, b], approved=False)
    await _resume(env, ids[0], approved=False, rejected=[])

    results = await _tool_results(env)
    assert set(results) == {a, b} and all("Rejected by user" in c for c in results.values())
    assert harness["ran"] == []


@pytest.mark.asyncio
async def test_a_resume_stopped_by_an_interrupt_still_answers_the_round(env, harness):
    """A continuation queued before an operator interrupt stops without
    running anything, but the round's calls still get results."""
    a, b = f"c-{uuid.uuid4().hex[:6]}", f"c-{uuid.uuid4().hex[:6]}"
    ids, _ = await _round(env, [a, b], approved=True)
    enqueued_at = datetime.now(timezone.utc).timestamp() - 5
    await chat_steering.request_interrupt(env["chat_id"], "tester")
    await execute_agent_resume_job(
        {},
        job_id=f"test-{uuid.uuid4().hex[:8]}",
        chat_id=env["chat_id"],
        user_id=env["user_id"],
        user_token="",
        pending_approval_id=ids[0],
        approved=True,
        rejected_tool_call_ids=[b],
        channel_id=f"chan-{uuid.uuid4().hex[:8]}",
        enqueued_at=enqueued_at,
    )

    results = await _tool_results(env)
    assert harness["ran"] == [] and harness["followups"] == 0
    assert set(results) == {a, b}
    assert "Interrupted" in results[a] and "Rejected by user" in results[b]
    async with AsyncSessionLocal() as s:
        roles = [
            m.role
            for m in (
                await s.execute(
                    select(Message).where(Message.chat_id == env["chat_id"]).order_by(Message.created_at)
                )
            ).scalars().all()
        ]
    assert roles == ["assistant", "tool", "tool", "system"]  # results directly after the calls


@pytest.mark.asyncio
async def test_a_round_left_to_expire_in_full_is_resumed_as_rejected(env, monkeypatch):
    """No decision at all before the deadline: the sweep resumes the round
    so the expired calls are answered as rejected, as the docs promise."""
    resumed = []

    async def fake_resume(**kwargs):
        resumed.append(kwargs)
        return "job"

    monkeypatch.setattr(queue_service, "enqueue_agent_resume", fake_resume)
    a, b = f"c-{uuid.uuid4().hex[:6]}", f"c-{uuid.uuid4().hex[:6]}"
    await _round(env, [a, b], expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))

    await dc.expire_due()
    assert len(resumed) == 1
    assert resumed[0]["approved"] is False
    assert sorted(resumed[0]["rejected_tool_call_ids"]) == sorted([a, b])
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(select(PendingToolApproval).where(PendingToolApproval.chat_id == env["chat_id"]))
        ).scalars().all()
    assert all(r.approved is False for r in rows)

    await dc.expire_due()
    assert len(resumed) == 1  # resolved: the next sweep has nothing to resume
