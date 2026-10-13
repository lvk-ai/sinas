"""MCP Tasks (the io.modelcontextprotocol/tasks extension, 2026-07-28).

A server may answer a tool call with a task handle instead of a result.
Sinas declares support on every request, polls a task inline for a short
while, and otherwise keeps it and polls it in the background, delivering
the result into the chat when it lands. The server here is a real SDK
server serving the extension in-process (tests/unit/mcp_tasks_server.py).
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from mcp.types import CallToolResult
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal, async_engine
from app.models.agent import Agent
from app.models.chat import Chat, Message
from app.models.mcp_server import McpServer
from app.models.mcp_task import McpTask
from app.models.user import Role, RolePermission, User, UserRole
from app.services import chat_wakeups, mcp_client, mcp_tasks
from app.services.mcp_client import TaskState, task_outcome
from app.services.mcp_tools import McpToolConverter
from tests.unit.mcp_tasks_server import TasksMcp

REAL_DELIVER = chat_wakeups.deliver_to_chat


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _in(**delta) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**delta)


@pytest_asyncio.fixture
async def tasks_mcp(monkeypatch):
    fake = TasksMcp()
    mcp_client.clear_tool_cache()
    monkeypatch.setattr(mcp_client, "open_transport", fake.open_transport)
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        async with fake.app.router.lifespan_context(fake.app):
            started.set()
            await stop.wait()

    runner = asyncio.create_task(run_lifespan())
    await started.wait()
    try:
        yield fake
    finally:
        stop.set()
        await runner
        mcp_client.clear_tool_cache()


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine_per_test():
    yield
    await async_engine.dispose()


@pytest_asyncio.fixture
async def server(db: AsyncSession, test_user: User) -> McpServer:
    row = McpServer(
        user_id=test_user.id, namespace="tools", name=f"tasks-{_uid()}", url="http://tasks.test/mcp",
        transport="streamable_http", auth={"type": "none"}, headers={}, tool_allow=[], tool_deny=[],
        timeout_seconds=30, connect_timeout_seconds=5, is_active=True,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


def _text(result: CallToolResult) -> str:
    return "".join(getattr(b, "text", "") for b in result.content)


class TestProtocol:
    async def test_a_current_server_is_spoken_to_in_the_2026_07_28_protocol(self, db, server, test_user, tasks_mcp):
        async with mcp_client.connect(db, server, str(test_user.id), read_timeout=5) as client:
            assert client.protocol_version == "2026-07-28"

    async def test_every_call_declares_tasks_and_plain_tools_still_answer_inline(self, db, server, test_user, tasks_mcp):
        result = await mcp_client.call_tool(db, server, "quick", {"text": "hi"}, str(test_user.id))
        assert isinstance(result, CallToolResult) and _text(result) == "quick: hi"
        assert tasks_mcp.tasks.declared == [True]

    async def test_a_short_task_is_polled_inline_to_its_result(self, db, server, test_user, tasks_mcp):
        result = await mcp_client.call_tool(
            db, server, "slow", {"text": "x"}, str(test_user.id), task_wait_seconds=5
        )
        assert isinstance(result, CallToolResult) and _text(result) == "done: x"
        # The call is routed by tool name; each tasks/get by the task id.
        (task_id,) = tasks_mcp.tasks.tasks
        assert tasks_mcp.mcp_names[0] == "slow"
        assert tasks_mcp.mcp_names[1:] and set(tasks_mcp.mcp_names[1:]) == {task_id}

    async def test_without_a_wait_the_task_comes_back_and_can_be_polled(self, db, server, test_user, tasks_mcp):
        task = await mcp_client.call_tool(db, server, "slow", {"text": "y"}, str(test_user.id))
        assert isinstance(task, TaskState) and task.status == "working"
        for _ in range(5):
            task = await mcp_client.get_task(db, server, str(test_user.id), task.task_id)
            if task.status == "completed":
                break
        assert task.status == "completed"
        assert _text(task_outcome(task)) == "done: y"

    async def test_input_required_then_update(self, db, server, test_user, tasks_mcp):
        task = await mcp_client.call_tool(db, server, "ask", {}, str(test_user.id), task_wait_seconds=5)
        assert isinstance(task, TaskState) and task.status == "input_required"
        assert set(task.input_requests) == {"confirm"}
        await mcp_client.update_task(db, server, str(test_user.id), task.task_id, {"confirm": {"action": "accept", "content": {"ok": True}}})
        task = await mcp_client.get_task(db, server, str(test_user.id), task.task_id)
        assert task.status == "completed" and "confirmed" in _text(task_outcome(task))

    async def test_cancel(self, db, server, test_user, tasks_mcp):
        task = await mcp_client.call_tool(db, server, "forever", {}, str(test_user.id))
        await mcp_client.cancel_task(db, server, str(test_user.id), task.task_id)
        task = await mcp_client.get_task(db, server, str(test_user.id), task.task_id)
        assert task.status == "cancelled"
        assert task_outcome(task).is_error

    async def test_a_failed_task_is_an_error_result(self, db, server, test_user, tasks_mcp):
        result = await mcp_client.call_tool(db, server, "boom", {}, str(test_user.id), task_wait_seconds=5)
        assert isinstance(result, CallToolResult) and result.is_error and "it broke" in _text(result)


# ------------------------------------------------------------ kept tasks


@pytest_asyncio.fixture
async def env(tasks_mcp, monkeypatch):
    """Committed user, server, agent and chat: the poller opens its own
    sessions. Deliveries into the chat are recorded instead of sent."""
    delivered: list[tuple[str, str]] = []

    async def fake_deliver(chat_id, content, *, source, api_key_id=None, guard=None, delivery_id=None):
        # Like the real one: the guard runs in the transaction that writes
        # the delivery, and a False withdraws it.
        async with AsyncSessionLocal() as session:
            if guard is not None and not await guard(session):
                await session.rollback()
                return {"status": "dropped", "reason": "withdrawn"}
            await session.commit()
        delivered.append((chat_id, content))
        return {"status": "injected"}

    monkeypatch.setattr(chat_wakeups, "deliver_to_chat", fake_deliver)
    monkeypatch.setattr(settings, "mcp_task_min_poll_seconds", 0)

    async def no_such_job(job_id):  # the arq pool isn't reachable from tests
        return None

    monkeypatch.setattr(chat_wakeups, "_job_state", no_such_job)

    async with AsyncSessionLocal() as s:
        user = User(email=f"tasks-{_uid()}@example.com")
        s.add(user)
        await s.flush()
        role = Role(name=f"tasks-role-{_uid()}", description="t")
        s.add(role)
        await s.flush()
        s.add(RolePermission(role_id=role.id, permission_key="sinas.*:all", permission_value=True))
        s.add(UserRole(role_id=role.id, user_id=user.id, active=True))
        server = McpServer(
            user_id=user.id, namespace="tools", name=f"tasks-{_uid()}", url="http://tasks.test/mcp",
            transport="streamable_http", auth={"type": "none"}, headers={}, tool_allow=[], tool_deny=[],
            timeout_seconds=30, connect_timeout_seconds=5, is_active=True,
        )
        s.add(server)
        await s.flush()
        agent = Agent(
            user_id=user.id, namespace=f"ns{_uid()}", name="tasks-agent",
            enabled_mcp_servers=[{"server": f"tools/{server.name}", "tools": []}],
        )
        s.add(agent)
        await s.flush()
        chat = Chat(user_id=user.id, agent_id=agent.id, title="tasks chat")
        s.add(chat)
        await s.commit()
        for row in (user, chat, agent, server):
            await s.refresh(row)
        out = {"user": user, "chat": chat, "agent": agent, "server": server, "role": role,
               "delivered": delivered, "fake": tasks_mcp}

    yield out

    async with AsyncSessionLocal() as s:
        await s.execute(delete(McpTask).where(McpTask.chat_id == chat.id))
        await s.execute(delete(Message).where(Message.chat_id == chat.id))
        await s.execute(delete(Chat).where(Chat.id == chat.id))
        await s.execute(delete(Agent).where(Agent.id == agent.id))
        await s.execute(delete(McpServer).where(McpServer.id == server.id))
        await s.execute(delete(UserRole).where(UserRole.user_id == user.id))
        await s.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
        await s.execute(delete(Role).where(Role.id == role.id))
        await s.execute(delete(User).where(User.id == user.id))
        await s.commit()


async def _call(env, tool: str, arguments=None, *, wait: int = 0) -> dict:
    """The agent calls `tool` on the server, as the tool round would."""
    settings_wait = settings.mcp_task_inline_wait_seconds
    settings.mcp_task_inline_wait_seconds = wait
    try:
        async with AsyncSessionLocal() as s:
            chat = await s.get(Chat, env["chat"].id)
            server = env["server"]
            return await McpToolConverter().execute_tool(
                s, f"mcp_tools__{server.name}__{tool}", arguments or {}, str(env["user"].id),
                metadata={"server_namespace": "tools", "server_name": server.name, "mcp_tool": tool},
                chat=chat, tool_call_id=f"call_{_uid()}",
            )
    finally:
        settings.mcp_task_inline_wait_seconds = settings_wait


async def _row(task_id: str) -> McpTask:
    async with AsyncSessionLocal() as s:
        return await s.get(McpTask, uuid.UUID(task_id))


async def _poll_until(task_id: str, done) -> McpTask:
    for _ in range(10):
        await mcp_tasks.poll_due_tasks(now=_in(minutes=5))
        row = await _row(task_id)
        if done(row):
            return row
    raise AssertionError(f"task {task_id} never got there: {row.status}")


class TestKeptTasks:
    async def test_a_task_that_finishes_inline_is_an_ordinary_result(self, env):
        result = await _call(env, "slow", {"text": "now"}, wait=5)
        assert result == {"text": "done: now"}
        async with AsyncSessionLocal() as s:
            assert (await s.execute(select(McpTask).where(McpTask.chat_id == env["chat"].id))).first() is None

    async def test_a_longer_task_is_kept_polled_and_delivered_once(self, env):
        handle = await _call(env, "slow", {"text": "later"})
        assert handle["status"] == "working" and "task_id" in handle
        # The agent gets Sinas's id, never the server's.
        assert handle["task_id"] not in env["fake"].tasks.tasks
        row = await _poll_until(handle["task_id"], lambda r: r.finished_at is not None)
        assert row.status == "completed" and row.result == {"text": "done: later"}
        ((chat_id, content),) = env["delivered"]
        assert chat_id == str(env["chat"].id)
        assert content.startswith(f"[mcp task {handle['task_id']} completed: tools/{env['server'].name}/slow]")
        assert "done: later" in content
        # Finished: further sweeps leave it alone.
        await mcp_tasks.poll_due_tasks(now=_in(minutes=10))
        assert len(env["delivered"]) == 1

    async def test_input_requests_reach_the_agent_once_and_its_answer_goes_back(self, env):
        handle = await _call(env, "ask")
        task_id = handle["task_id"]
        row = await _poll_until(task_id, lambda r: r.status == "input_required")
        assert set(row.input_requests) == {"confirm"}
        ((_, content),) = env["delivered"]
        assert content.startswith(f"[mcp task {task_id} needs input") and "Really do it?" in content
        await mcp_tasks.poll_due_tasks(now=_in(minutes=5))
        assert len(env["delivered"]) == 1  # not again

        async with AsyncSessionLocal() as s:
            chat = await s.get(Chat, env["chat"].id)
            wrong = await mcp_tasks.execute_tool(s, chat, str(env["user"].id), mcp_tasks.MCP_TASK_RESPOND,
                                                 {"task_id": task_id, "responses": {"other": {}}})
            assert "Not waiting for: other" in wrong["error"]
            ok = await mcp_tasks.execute_tool(s, chat, str(env["user"].id), mcp_tasks.MCP_TASK_RESPOND,
                                              {"task_id": task_id, "responses": {"confirm": {"action": "accept", "content": {"ok": True}}}})
        assert ok["status"] == "working"
        assert env["fake"].tasks.updates[0]["responses"] == {"confirm": {"action": "accept", "content": {"ok": True}}}
        row = await _poll_until(task_id, lambda r: r.finished_at is not None)
        assert row.status == "completed" and "confirmed" in row.result["text"]

    async def test_status_is_a_live_look_that_delivers_nothing(self, env):
        handle = await _call(env, "slow", {"text": "s"})
        async with AsyncSessionLocal() as s:
            chat = await s.get(Chat, env["chat"].id)
            for _ in range(3):
                status = await mcp_tasks.execute_tool(s, chat, str(env["user"].id), mcp_tasks.MCP_TASK_STATUS,
                                                      {"task_id": handle["task_id"]})
                if status["status"] == "completed":
                    break
        assert status["status"] == "completed" and status["result"] == {"text": "done: s"}
        assert env["delivered"] == []

    async def test_cancel_stops_following_the_task_and_tells_the_server(self, env):
        handle = await _call(env, "forever")
        async with AsyncSessionLocal() as s:
            chat = await s.get(Chat, env["chat"].id)
            out = await mcp_tasks.execute_tool(s, chat, str(env["user"].id), mcp_tasks.MCP_TASK_CANCEL,
                                               {"task_id": handle["task_id"]})
        assert out["status"] == "cancelled"
        assert env["fake"].tasks.cancelled  # the server was told
        await mcp_tasks.poll_due_tasks(now=_in(minutes=5))
        assert env["delivered"] == []

    async def test_a_failed_task_is_delivered_as_an_error(self, env):
        handle = await _call(env, "boom")
        row = await _poll_until(handle["task_id"], lambda r: r.finished_at is not None)
        assert row.status == "failed" and "it broke" in row.result["error"]
        assert "failed" in env["delivered"][0][1]

    async def test_another_chat_cannot_touch_the_task(self, env, db, test_user):
        handle = await _call(env, "forever")
        other = Chat(user_id=test_user.id, title="other")
        db.add(other)
        await db.flush()
        out = await mcp_tasks.execute_tool(db, other, str(test_user.id), mcp_tasks.MCP_TASK_CANCEL,
                                           {"task_id": handle["task_id"]})
        assert out == {"error": "Unknown task_id for this chat"}

    async def test_losing_access_to_the_server_ends_the_task(self, env):
        handle = await _call(env, "forever")
        async with AsyncSessionLocal() as s:
            server = await s.get(McpServer, env["server"].id)
            server.is_active = False
            await s.commit()
        row = await _poll_until(handle["task_id"], lambda r: r.finished_at is not None)
        assert row.status == "failed" and "not found or inactive" in row.result["error"]

    async def test_unreachable_server_backs_off_then_gives_up(self, env, monkeypatch):
        handle = await _call(env, "forever")
        monkeypatch.setattr(settings, "mcp_task_max_poll_failures", 2)

        async def down(*a, **k):
            raise mcp_client.McpClientError("connection refused")

        monkeypatch.setattr(mcp_client, "get_task", down)
        row = await _poll_until(handle["task_id"], lambda r: r.finished_at is not None)
        assert row.status == "failed" and "Lost contact" in row.result["error"]

    async def test_a_failed_inline_poll_keeps_the_task_instead_of_losing_it(self, env):
        env["fake"].tasks.fail_gets = 1
        handle = await _call(env, "slow", {"text": "kept"}, wait=5)
        assert handle["status"] == "working" and "task_id" in handle
        row = await _poll_until(handle["task_id"], lambda r: r.finished_at is not None)
        assert row.result == {"text": "done: kept"}

    async def test_a_slow_answer_does_not_stretch_the_inline_wait(self, env):
        env["fake"].tasks.get_delay = 3
        started = asyncio.get_running_loop().time()
        handle = await _call(env, "slow", {"text": "z"}, wait=1)
        assert asyncio.get_running_loop().time() - started < 2.5
        assert handle["status"] == "working"

    async def test_a_delivery_that_fails_is_kept_and_retried(self, env, monkeypatch):
        calls = {"n": 0}
        real = chat_wakeups.deliver_to_chat

        async def flaky(chat_id, content, *, source, api_key_id=None, guard=None, delivery_id=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("redis is down")
            return await real(chat_id, content, source=source, api_key_id=api_key_id, guard=guard, delivery_id=delivery_id)

        monkeypatch.setattr(chat_wakeups, "deliver_to_chat", flaky)
        handle = await _call(env, "slow", {"text": "r"})
        row = await _poll_until(handle["task_id"], lambda r: r.finished_at is not None)
        # Finished, but the result has not reached the chat yet: still owed.
        assert env["delivered"] == [] and len(row.pending_deliveries) == 1 and row.next_poll_at is not None
        await mcp_tasks.poll_due_tasks(now=_in(minutes=5))
        row = await _row(handle["task_id"])
        assert len(env["delivered"]) == 1 and "done: r" in env["delivered"][0][1]
        assert row.pending_deliveries == [] and row.next_poll_at is None

    async def test_a_poll_that_returns_after_a_cancel_changes_nothing(self, env, monkeypatch):
        handle = await _call(env, "ask")
        real_get = mcp_client.get_task

        async def get_then_cancelled(db, server, user_id, task_id):
            state = await real_get(db, server, user_id, task_id)  # input_required
            async with AsyncSessionLocal() as s:  # the agent cancels meanwhile
                chat = await s.get(Chat, env["chat"].id)
                await mcp_tasks.execute_tool(s, chat, str(env["user"].id), mcp_tasks.MCP_TASK_CANCEL,
                                             {"task_id": handle["task_id"]})
            return state

        monkeypatch.setattr(mcp_client, "get_task", get_then_cancelled)
        await mcp_tasks.poll_due_tasks(now=_in(minutes=5))
        row = await _row(handle["task_id"])
        assert row.status == "cancelled" and env["delivered"] == []

    async def test_two_flushes_at_once_deliver_a_message_once(self, env, monkeypatch):
        handle = await _call(env, "ask")
        task_id = uuid.UUID(handle["task_id"])
        real = chat_wakeups.deliver_to_chat
        both_in = asyncio.Event()
        arrived = 0

        async def side_by_side(chat_id, content, *, source, api_key_id=None, guard=None, delivery_id=None):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both_in.set()
            await asyncio.wait_for(both_in.wait(), 5)  # both hold the same message
            return await real(chat_id, content, source=source, api_key_id=api_key_id, guard=guard, delivery_id=delivery_id)

        await _poll_until(handle["task_id"], lambda r: r.status == "input_required" or bool(r.pending_deliveries))
        env["delivered"].clear()
        async with AsyncSessionLocal() as s:  # make sure one is owed
            row = await s.get(McpTask, task_id)
            row.pending_deliveries = [{"id": "m1", "content": "[mcp task x needs input]"}]
            await s.commit()
        monkeypatch.setattr(chat_wakeups, "deliver_to_chat", side_by_side)
        await asyncio.gather(mcp_tasks._flush_deliveries(task_id), mcp_tasks._flush_deliveries(task_id))
        assert len(env["delivered"]) == 1
        assert (await _row(handle["task_id"])).pending_deliveries == []

    async def test_a_message_in_flight_when_the_task_is_cancelled_is_not_delivered(self, env, monkeypatch):
        handle = await _call(env, "ask")
        task_id = uuid.UUID(handle["task_id"])
        async with AsyncSessionLocal() as s:
            row = await s.get(McpTask, task_id)
            row.pending_deliveries = [{"id": "m1", "content": "[mcp task x needs input]"}]
            await s.commit()
        real = chat_wakeups.deliver_to_chat

        async def cancelled_meanwhile(chat_id, content, *, source, api_key_id=None, guard=None, delivery_id=None):
            async with AsyncSessionLocal() as s:  # the agent cancels while it is on its way
                chat = await s.get(Chat, env["chat"].id)
                await mcp_tasks.execute_tool(s, chat, str(env["user"].id), mcp_tasks.MCP_TASK_CANCEL,
                                             {"task_id": handle["task_id"]})
            return await real(chat_id, content, source=source, api_key_id=api_key_id, guard=guard, delivery_id=delivery_id)

        monkeypatch.setattr(chat_wakeups, "deliver_to_chat", cancelled_meanwhile)
        await mcp_tasks._flush_deliveries(task_id)
        assert env["delivered"] == []
        assert (await _row(handle["task_id"])).status == "cancelled"

    async def _owe(self, env, task_id: uuid.UUID) -> None:
        async with AsyncSessionLocal() as s:
            row = await s.get(McpTask, task_id)
            row.pending_deliveries = [{"id": "m1", "content": "[mcp task x completed]"}]
            await s.commit()

    async def test_a_new_turn_that_cannot_be_queued_leaves_the_message_owed(self, env, monkeypatch):
        """Idle chat: delivery starts a turn. The turn is queued inside the
        transaction that takes the message off; a failed enqueue rolls that
        back."""
        from app.services.queue_service import queue_service

        handle = await _call(env, "forever")
        task_id = uuid.UUID(handle["task_id"])
        await self._owe(env, task_id)
        monkeypatch.setattr(chat_wakeups, "deliver_to_chat", REAL_DELIVER)

        async def down(**kwargs):
            raise RuntimeError("redis is down")

        monkeypatch.setattr(queue_service, "enqueue_agent_message", down)
        await mcp_tasks._flush_deliveries(task_id)
        row = await _row(handle["task_id"])
        assert [e["id"] for e in row.pending_deliveries] == ["m1"]
        async with AsyncSessionLocal() as s:
            assert (await s.get(Chat, env["chat"].id)).active_channel_id is None

    async def test_a_retry_after_a_crash_reuses_the_job_id(self, env, monkeypatch):
        """Queued, then the worker died before committing: the retry asks
        for the same job id, which arq refuses to queue twice."""
        from app.services.queue_service import queue_service

        handle = await _call(env, "forever")
        task_id = uuid.UUID(handle["task_id"])
        await self._owe(env, task_id)
        monkeypatch.setattr(chat_wakeups, "deliver_to_chat", REAL_DELIVER)
        job_ids = []

        async def queued_then_crashed(**kwargs):
            job_ids.append(kwargs["job_id"])
            if len(job_ids) == 1:
                raise RuntimeError("worker died before the commit")
            return kwargs["job_id"]

        monkeypatch.setattr(queue_service, "enqueue_agent_message", queued_then_crashed)
        await mcp_tasks._flush_deliveries(task_id)  # the "crash": rolled back, still owed
        await mcp_tasks._flush_deliveries(task_id)  # the retry
        assert len(job_ids) == 2 and job_ids[0] == job_ids[1] and job_ids[0].startswith("delivery-")
        assert (await _row(handle["task_id"])).pending_deliveries == []

    async def test_after_a_crash_whose_turn_already_runs_the_retry_does_not_inject_it_again(self, env, monkeypatch):
        """Queued, the worker died before committing, and that turn now
        holds the chat: the retry must not inject the message a second
        time, only take it off."""
        from app.services import chat_steering
        from app.services.queue_service import queue_service

        handle = await _call(env, "forever")
        task_id = uuid.UUID(handle["task_id"])
        await self._owe(env, task_id)
        monkeypatch.setattr(chat_wakeups, "deliver_to_chat", REAL_DELIVER)
        queued = []

        async def queued_then_crashed(**kwargs):
            queued.append(kwargs["job_id"])
            raise RuntimeError("worker died before the commit")

        monkeypatch.setattr(queue_service, "enqueue_agent_message", queued_then_crashed)
        await mcp_tasks._flush_deliveries(task_id)
        assert len(queued) == 1

        async def known(job_id):
            return "running" if job_id == queued[0] else None

        async def locked(chat_id):
            return True  # the queued turn is running

        monkeypatch.setattr(chat_wakeups, "_job_state", known)
        monkeypatch.setattr(chat_steering, "is_chat_locked", locked)
        await mcp_tasks._flush_deliveries(task_id)
        assert (await _row(handle["task_id"])).pending_deliveries == []
        async with AsyncSessionLocal() as s:
            injected = (await s.execute(
                select(Message).where(Message.chat_id == env["chat"].id, Message.role == "user")
            )).scalars().all()
        assert injected == []
        # The queued turn's stream channel is recorded, so a client can reconnect.
        async with AsyncSessionLocal() as s:
            chat = await s.get(Chat, env["chat"].id)
            assert chat.active_channel_id and queued[0] == f"delivery-{chat.active_channel_id}"

    async def test_a_finished_earlier_turn_leaves_a_newer_turns_channel_alone(self, env, monkeypatch):
        from app.services.queue_service import queue_service

        handle = await _call(env, "forever")
        task_id = uuid.UUID(handle["task_id"])
        await self._owe(env, task_id)
        monkeypatch.setattr(chat_wakeups, "deliver_to_chat", REAL_DELIVER)

        async def queued_then_crashed(**kwargs):
            raise RuntimeError("worker died before the commit")

        monkeypatch.setattr(queue_service, "enqueue_agent_message", queued_then_crashed)
        await mcp_tasks._flush_deliveries(task_id)
        async with AsyncSessionLocal() as s:  # meanwhile: that turn ran, a newer one started
            chat = await s.get(Chat, env["chat"].id)
            chat.active_channel_id = "newer-turn"
            await s.commit()

        async def finished(job_id):
            return "finished"

        monkeypatch.setattr(chat_wakeups, "_job_state", finished)
        await mcp_tasks._flush_deliveries(task_id)
        assert (await _row(handle["task_id"])).pending_deliveries == []
        async with AsyncSessionLocal() as s:
            assert (await s.get(Chat, env["chat"].id)).active_channel_id == "newer-turn"

    async def test_the_agent_gets_the_task_tools_with_an_mcp_server(self):
        names = {t["function"]["name"] for t in mcp_tasks.get_tool_definitions()}
        assert names == {"mcp_task_status", "mcp_task_respond", "mcp_task_cancel"}
