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

    async def fake_deliver(chat_id, content, *, source, api_key_id=None):
        delivered.append((chat_id, content))
        return {"status": "injected"}

    monkeypatch.setattr(chat_wakeups, "deliver_to_chat", fake_deliver)
    monkeypatch.setattr(settings, "mcp_task_min_poll_seconds", 0)

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

    async def test_the_agent_gets_the_task_tools_with_an_mcp_server(self):
        names = {t["function"]["name"] for t in mcp_tasks.get_tool_definitions()}
        assert names == {"mcp_task_status", "mcp_task_respond", "mcp_task_cancel"}
