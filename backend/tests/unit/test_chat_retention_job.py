"""Chat retention job: selection, archive-then-delete, skips, failures,
concurrency, the rows that hang off a chat, the dry run, restore.

These use real connections and real commits (the job opens its own
sessions, claims chats with advisory locks in their own transactions and
deletes in short transactions of its own; the `db` fixture's single
rolled-back transaction can't show any of that), like the setup-link race
tests. Each test removes what it created.
"""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.agent import Agent
from app.models.chat import Chat, Message
from app.models.chat_wakeup import TIMER, WATCH, ChatWakeup
from app.models.execution import Execution, ExecutionStatus, TriggerType
from app.models.file import Collection, File, FileVersion
from app.models.llm_usage import LLMUsage
from app.models.mcp_server import McpServer
from app.models.mcp_task import McpTask
from app.models.pending_approval import PendingToolApproval
from app.models.pending_completion import PendingCompletion
from app.models.retention_run import ChatRetentionRun
from app.models.tool_call_result import ToolCallResult
from app.models.user import User
from app.services.retention import job
from app.services.retention.archive import parse_archive
from app.services.retention.restore import apply_restore, plan_restore
from app.services.retention.store import ArchiveUploadError
from tests.conftest import _test_db_url

NOW = datetime.now(UTC)


class FakeStore:
    """Records PutObject calls; can fail, be slow, or run a hook first."""

    def __init__(self, *, fail=False, delay=0.0, on_put=None):
        self.fail = fail
        self.delay = delay
        self.on_put = on_put
        self.puts: list[str] = []
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, dict] = {}

    async def put(self, key, body, *, content_type, metadata):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.on_put:
            await self.on_put(key)
        if self.fail:
            raise ArchiveUploadError("PutObject refused: AccessDenied (HTTP 403)")
        self.puts.append(key)
        self.objects[key] = body
        self.metadata[key] = metadata


@pytest.fixture(autouse=True)
def _retention_on(monkeypatch):
    monkeypatch.setattr(settings, "chat_retention_enabled", True)
    monkeypatch.setattr(settings, "chat_retention_days", 30)
    monkeypatch.setattr(settings, "chat_retention_action", "archive")
    monkeypatch.setattr(settings, "chat_retention_batch_size", 200)
    monkeypatch.setattr(settings, "chat_retention_max_chats_per_run", 0)
    monkeypatch.setattr(settings, "chat_retention_max_run_seconds", 3300)
    monkeypatch.setattr(settings, "chat_archive_s3_bucket", None)
    monkeypatch.setattr(settings, "chat_archive_s3_prefix", "sinas/chats")


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(_test_db_url)
    yield eng
    await eng.dispose()


class World:
    """Creates users, agents and chats with chosen ages; removes them after."""

    def __init__(self, engine):
        self.engine = engine
        self.sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        self.user_ids: list[uuid.UUID] = []
        self.agent_ids: list[uuid.UUID] = []
        self.server_ids: list[uuid.UUID] = []
        self.user_id: uuid.UUID | None = None

    async def setup(self):
        self.user_id = await self.user()

    async def user(self) -> uuid.UUID:
        async with self.sessions() as db:
            user = User(email=f"ret-{uuid.uuid4().hex[:10]}@example.com")
            db.add(user)
            await db.commit()
            self.user_ids.append(user.id)
            return user.id

    async def agent(self, retention_days=None) -> Agent:
        async with self.sessions() as db:
            agent = Agent(
                namespace="rettest", name=f"a{uuid.uuid4().hex[:8]}", user_id=self.user_id,
                retention_days=retention_days,
            )
            db.add(agent)
            await db.commit()
            self.agent_ids.append(agent.id)
            return agent

    async def chat(self, agent: Agent | None = None, *, idle_days: float = 100, messages: int = 2,
                   last_message_days: float | None = None, user_id=None) -> uuid.UUID:
        """A chat last updated `idle_days` ago whose newest message is
        `last_message_days` ago (default: as old as the chat)."""
        updated = NOW - timedelta(days=idle_days)
        newest = NOW - timedelta(days=last_message_days if last_message_days is not None else idle_days)
        async with self.sessions() as db:
            chat = Chat(
                user_id=user_id or self.user_id,
                agent_id=agent.id if agent else None,
                agent_namespace=agent.namespace if agent else None,
                agent_name=agent.name if agent else None,
                title="retention test chat",
                created_at=updated - timedelta(days=1),
                updated_at=updated,
            )
            db.add(chat)
            await db.flush()
            for i in range(messages):
                db.add(Message(
                    chat_id=chat.id, role="user" if i % 2 == 0 else "assistant",
                    content=f"message {i}",
                    created_at=newest - timedelta(minutes=messages - i),
                ))
            await db.commit()
            return chat.id

    async def exists(self, chat_id) -> bool:
        async with self.sessions() as db:
            return (await db.execute(select(Chat.id).where(Chat.id == chat_id))).scalar_one_or_none() is not None

    async def count(self, model, *where) -> int:
        async with self.sessions() as db:
            return (await db.execute(select(func.count()).select_from(model).where(*where))).scalar()

    async def add(self, *objs):
        async with self.sessions() as db:
            for obj in objs:
                db.add(obj)
                await db.flush()
            await db.commit()

    async def mcp_server(self) -> uuid.UUID:
        server = McpServer(user_id=self.user_id, namespace="rettest", name=f"s{uuid.uuid4().hex[:8]}",
                           url="https://mcp.example.test")
        await self.add(server)
        self.server_ids.append(server.id)
        return server.id

    async def cleanup(self):
        async with self.sessions() as db:
            chat_ids = (await db.execute(select(Chat.id).where(Chat.user_id.in_(self.user_ids)))).scalars().all()
            if chat_ids:
                await db.execute(delete(PendingToolApproval).where(PendingToolApproval.chat_id.in_(chat_ids)))
                await db.execute(delete(PendingCompletion).where(PendingCompletion.chat_id.in_(chat_ids)))
                await db.execute(delete(Execution).where(Execution.chat_id.in_(chat_ids)))
                await db.execute(delete(ToolCallResult).where(ToolCallResult.chat_id.in_(chat_ids)))
                await db.execute(delete(Collection).where(Collection.name.in_([str(c) for c in chat_ids])))
                await db.execute(delete(Message).where(Message.chat_id.in_(chat_ids)))
                await db.execute(delete(Chat).where(Chat.id.in_(chat_ids)))
            await db.execute(delete(Execution).where(Execution.user_id.in_(self.user_ids)))
            await db.execute(delete(LLMUsage).where(LLMUsage.user_id.in_(self.user_ids)))
            await db.execute(delete(Collection).where(Collection.user_id.in_(self.user_ids)))
            await db.execute(delete(McpServer).where(McpServer.id.in_(self.server_ids)))
            await db.execute(delete(Agent).where(Agent.id.in_(self.agent_ids)))
            await db.execute(delete(User).where(User.id.in_(self.user_ids)))
            await db.execute(delete(ChatRetentionRun))
            await db.commit()


@pytest_asyncio.fixture
async def world(engine, monkeypatch):
    w = World(engine)
    await w.setup()
    # Runs see only this test's users' chats: the database is shared with
    # every other test, and old chats another test left behind are not ours
    # to archive or delete (nor to count).
    real = job.eligible_stmt

    def scoped(*args, **kwargs):
        return real(*args, **kwargs).where(Chat.user_id.in_(w.user_ids))

    monkeypatch.setattr(job, "eligible_stmt", scoped)
    try:
        yield w
    finally:
        await w.cleanup()


async def run(world: World, store=None, **kw):
    return await job.run_retention(trigger="cli", store=store, engine=world.engine, **kw)


# ---------------------------------------------------------------------------
# Policy and age
# ---------------------------------------------------------------------------


class TestPolicyAndAge:
    async def test_global_default_archives_then_deletes_old_chats_only(self, world):
        old = await world.chat(idle_days=40)
        recent = await world.chat(idle_days=10)
        store = FakeStore()
        result = await run(world, store)
        assert result["status"] == "completed"
        assert not await world.exists(old)
        assert await world.exists(recent)
        assert len(store.puts) == 1 and str(old) in store.puts[0]
        assert await world.count(Message, Message.chat_id == old) == 0

    async def test_age_is_last_activity_not_creation(self, world):
        # Chat row untouched for 200 days, but a message 2 days ago: in use.
        active = await world.chat(idle_days=200, last_message_days=2)
        store = FakeStore()
        await run(world, store)
        assert await world.exists(active)
        assert store.puts == []

    async def test_agent_override_never_and_default(self, world):
        short = await world.agent(retention_days=5)
        never = await world.agent(retention_days=0)
        default = await world.agent(retention_days=None)
        c_short = await world.chat(short, idle_days=8)
        c_never = await world.chat(never, idle_days=1000)
        c_default_young = await world.chat(default, idle_days=20)
        c_default_old = await world.chat(default, idle_days=40)
        c_no_agent = await world.chat(None, idle_days=40)
        store = FakeStore()
        result = await run(world, store)
        assert not await world.exists(c_short)
        assert await world.exists(c_never)
        assert await world.exists(c_default_young)
        assert not await world.exists(c_default_old)
        assert not await world.exists(c_no_agent)
        assert result["per_agent"][f"rettest/{short.name}"]["deleted"] == 1
        assert result["per_agent"]["(no agent)"]["deleted"] == 1

    async def test_no_global_default_only_agent_overrides_apply(self, world, monkeypatch):
        monkeypatch.setattr(settings, "chat_retention_days", 0)
        short = await world.agent(retention_days=5)
        c_short = await world.chat(short, idle_days=8)
        c_other = await world.chat(None, idle_days=5000)
        await run(world, FakeStore())
        assert not await world.exists(c_short)
        assert await world.exists(c_other)

    async def test_disabled_refuses_and_deletes_nothing(self, world, monkeypatch):
        monkeypatch.setattr(settings, "chat_retention_enabled", False)
        old = await world.chat(idle_days=400)
        result = await run(world, FakeStore())
        assert result["status"] == "refused"
        assert await world.exists(old)

    async def test_archive_without_destination_refuses(self, world):
        old = await world.chat(idle_days=400)
        result = await run(world, None)
        assert result["status"] == "refused"
        assert "destination" in result["error"]
        assert await world.exists(old)

    async def test_delete_action_without_destination_deletes(self, world, monkeypatch):
        monkeypatch.setattr(settings, "chat_retention_action", "delete")
        old = await world.chat(idle_days=400)
        result = await run(world, None)
        assert result["status"] == "completed"
        assert result["counts"]["archived"] == 0 and result["counts"]["deleted"] == 1
        assert not await world.exists(old)

    async def test_delete_action_still_archives_when_a_destination_is_set(self, world, monkeypatch):
        monkeypatch.setattr(settings, "chat_retention_action", "delete")
        old = await world.chat(idle_days=400)
        store = FakeStore()
        await run(world, store)
        assert len(store.puts) == 1
        assert not await world.exists(old)


# ---------------------------------------------------------------------------
# Chats that must be left alone
# ---------------------------------------------------------------------------


class TestSkips:
    async def _first_message(self, world, chat_id):
        async with world.sessions() as db:
            return (await db.execute(select(Message.id).where(Message.chat_id == chat_id).limit(1))).scalar_one()

    async def test_parked_pending_and_live_wakeups_are_skipped(self, world):
        parked = await world.chat(idle_days=100)
        pending = await world.chat(idle_days=100)
        timer = await world.chat(idle_days=100)
        watch = await world.chat(idle_days=100)
        task = await world.chat(idle_days=100)
        server = await world.mcp_server()
        await world.add(
            PendingCompletion(chat_id=parked, user_id=world.user_id, channel_id="c", pending={"t": {}},
                              results={}, remaining=1, conversation_context={}),
            PendingToolApproval(chat_id=pending, message_id=await self._first_message(world, pending),
                                user_id=world.user_id, tool_call_id="call_p", function_namespace="d",
                                function_name="f", arguments={}, all_tool_calls=[], conversation_context={}),
            ChatWakeup(chat_id=timer, user_id=world.user_id, kind=TIMER, due_at=NOW + timedelta(days=1)),
            ChatWakeup(chat_id=watch, user_id=world.user_id, kind=WATCH, token=uuid.uuid4().hex),
            McpTask(chat_id=task, user_id=world.user_id, mcp_server_id=server, server_task_id="st",
                    tool_name="t", model_tool_name="t", status="working"),
        )
        store = FakeStore()
        result = await run(world, store)
        for chat_id in (parked, pending, timer, watch, task):
            assert await world.exists(chat_id)
        assert store.puts == []
        assert result["counts"]["skipped"] == {
            "parked": 1, "pending_approval": 1, "wakeup": 2, "mcp_task": 1,
        }

    async def test_running_turn_and_background_job_are_skipped(self, world):
        from app.core.redis import get_redis

        running = await world.chat(idle_days=100)
        background = await world.chat(idle_days=100)
        redis = await get_redis()
        await redis.set(f"chat:loop-lock:{running}", "token", ex=60)
        await redis.sadd(f"sinas:chat:bgjobs:{background}", "job1")
        await redis.set("sinas:bgjob:job1", json.dumps({"status": "running", "chat_id": str(background)}))
        result = await run(world, FakeStore())
        assert await world.exists(running) and await world.exists(background)
        assert result["counts"]["skipped"] == {"running": 1, "background_job": 1}

    async def test_spent_state_does_not_block(self, world):
        chat_id = await world.chat(idle_days=100)
        server = await world.mcp_server()
        await world.add(
            PendingToolApproval(chat_id=chat_id, message_id=await self._first_message(world, chat_id),
                                user_id=world.user_id, tool_call_id="call_a", function_namespace="d",
                                function_name="f", arguments={}, all_tool_calls=[], conversation_context={},
                                approved=True),
            ChatWakeup(chat_id=chat_id, user_id=world.user_id, kind=TIMER, due_at=NOW - timedelta(days=99),
                       delivered_at=NOW - timedelta(days=99)),
            ChatWakeup(chat_id=chat_id, user_id=world.user_id, kind=WATCH, token=uuid.uuid4().hex,
                       expires_at=NOW - timedelta(days=1)),
            McpTask(chat_id=chat_id, user_id=world.user_id, mcp_server_id=server, server_task_id="st",
                    tool_name="t", model_tool_name="t", status="completed", finished_at=NOW - timedelta(days=99)),
        )
        await run(world, FakeStore())
        assert not await world.exists(chat_id)


# ---------------------------------------------------------------------------
# Rows that hang off a chat
# ---------------------------------------------------------------------------


class TestDependentRows:
    async def test_everything_owned_goes_executions_unlinked_usage_kept(self, world, monkeypatch, tmp_path):
        import app.services.file_storage as file_storage
        from app.services.tool_result_store import ensure_partitions

        monkeypatch.setattr(file_storage, "_storage", file_storage.LocalFileStorage(str(tmp_path)))
        chat_id = await world.chat(idle_days=100)
        async with world.sessions() as db:
            await ensure_partitions(db)
            message_id = (await db.execute(select(Message.id).where(Message.chat_id == chat_id).limit(1))).scalar_one()
        server = await world.mcp_server()
        execution_id = f"exec-{uuid.uuid4().hex}"
        usage_id = uuid.uuid4()
        workbench = Collection(namespace="_chat", name=str(chat_id), kind="workbench", user_id=world.user_id)
        await world.add(workbench)
        file = File(collection_id=workbench.id, name="notes.md", user_id=world.user_id, content_type="text/markdown")
        await world.add(file)
        storage_path = f"_chat/{file.id}/v1"
        await file_storage.get_storage().save(storage_path, b"workbench bytes")
        await world.add(
            FileVersion(file_id=file.id, version_number=1, storage_path=storage_path, size_bytes=15,
                        hash_sha256="ab" * 32, uploaded_by=world.user_id),
            PendingToolApproval(chat_id=chat_id, message_id=message_id, user_id=world.user_id,
                                tool_call_id="call_r", function_namespace="d", function_name="f", arguments={},
                                all_tool_calls=[], conversation_context={}, approved=False),
            ChatWakeup(chat_id=chat_id, user_id=world.user_id, kind=TIMER, cancelled=True),
            McpTask(chat_id=chat_id, user_id=world.user_id, mcp_server_id=server, server_task_id="st",
                    tool_name="t", model_tool_name="t", status="completed", finished_at=NOW),
            Execution(user_id=world.user_id, execution_id=execution_id, function_name="f",
                      trigger_type=TriggerType.AGENT, trigger_id="x", chat_id=chat_id,
                      status=ExecutionStatus.COMPLETED, input_data={}, started_at=NOW - timedelta(days=100)),
            ToolCallResult(tool_call_id="call_r", chat_id=chat_id, user_id=world.user_id, tool_name="f",
                           result={"v": 1}, source="agent", created_at=NOW, expires_at=NOW + timedelta(days=30)),
            LLMUsage(id=usage_id, user_id=world.user_id, chat_id=chat_id, message_id=message_id,
                     prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        store = FakeStore()
        result = await run(world, store)
        assert result["counts"]["deleted"] == 1, result
        assert not await world.exists(chat_id)
        for model in (Message, PendingToolApproval, ChatWakeup, McpTask, ToolCallResult):
            assert await world.count(model, model.chat_id == chat_id) == 0, model
        assert await world.count(Collection, Collection.id == workbench.id) == 0
        assert not (tmp_path / storage_path).exists()
        # The execution record stays, unlinked; usage is untouched.
        async with world.sessions() as db:
            execution = (await db.execute(select(Execution).where(Execution.execution_id == execution_id))).scalar_one()
            assert execution.chat_id is None
            usage = await db.get(LLMUsage, usage_id)
            assert usage is not None and usage.chat_id == chat_id

        archive = parse_archive(next(iter(store.objects.values())))
        assert len(archive.tool_results) == 1
        assert archive.header["workbench"]["files"][0]["path"] == "notes.md"
        text = json.dumps({"h": archive.header, "c": archive.chat})
        assert "token" not in text and "active_channel_id" not in text


# ---------------------------------------------------------------------------
# Archive content and restore
# ---------------------------------------------------------------------------


class TestArchiveAndRestore:
    async def test_archive_content_round_trips_through_restore(self, world):
        agent = await world.agent(retention_days=10)
        chat_id = await world.chat(agent, idle_days=20, messages=3)
        async with world.sessions() as db:
            before = [
                (m.id, m.role, m.content, m.created_at)
                for m in (await db.execute(
                    select(Message).where(Message.chat_id == chat_id).order_by(Message.created_at)
                )).scalars()
            ]
        store = FakeStore()
        await run(world, store)
        assert not await world.exists(chat_id)

        key, body = next(iter(store.objects.items()))
        created = (NOW - timedelta(days=21))
        assert key == f"sinas/chats/{created:%Y}/{created:%m}/{chat_id}.jsonl.gz"
        assert store.metadata[key]["sinas-chat-id"] == str(chat_id)
        archive = parse_archive(body)
        assert archive.chat["id"] == str(chat_id)
        assert archive.chat["agent_namespace"] == "rettest"
        assert archive.header["retention"]["days"] == 10
        assert archive.header["retention"]["source"] == "agent"
        assert [m["content"] for m in archive.messages] == [b[2] for b in before]

        async with world.sessions() as db:
            plan = await plan_restore(db, archive)
            assert plan.ok, plan.conflicts
            await apply_restore(db, archive, plan)
            await db.commit()
        async with world.sessions() as db:
            restored = await db.get(Chat, chat_id)
            assert restored.agent_id == agent.id
            assert restored.updated_at > NOW - timedelta(minutes=5)  # a full retention period again
            after = [
                (m.id, m.role, m.content, m.created_at)
                for m in (await db.execute(
                    select(Message).where(Message.chat_id == chat_id).order_by(Message.created_at)
                )).scalars()
            ]
        assert after == before

        # Restoring it again conflicts (the chat is there) and writes nothing.
        async with world.sessions() as db:
            plan = await plan_restore(db, archive)
            assert not plan.ok
        # And the next run doesn't take it straight back out.
        store2 = FakeStore()
        await run(world, store2)
        assert await world.exists(chat_id)

    async def test_restore_cli_dry_run_then_restore(self, world, tmp_path):
        import sys

        chat_id = await world.chat(idle_days=100, messages=2)
        store = FakeStore()
        await run(world, store)
        path = tmp_path / "chat.jsonl.gz"
        path.write_bytes(next(iter(store.objects.values())))

        async def cli(*args):
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "app.cli", "restore-chat", str(path), *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            return proc.returncode, out.decode(), err.decode()

        code, out, err = await cli("--dry-run")
        assert code == 0, err
        report = json.loads(out)
        assert report["valid"] and report["dry_run"] and report["messages"] == 2
        assert not await world.exists(chat_id)

        code, out, err = await cli()
        assert code == 0, err
        assert json.loads(out)["restored"] is True
        assert await world.exists(chat_id)

        # A corrupted copy is refused before anything is written.
        path.write_bytes(path.read_bytes()[:-20])
        code, out, err = await cli("--dry-run")
        assert code == 1 and "invalid archive" in err

    async def test_restore_to_another_user_when_the_owner_is_gone(self, world):
        other = await world.user()
        chat_id = await world.chat(idle_days=100, user_id=other)
        store = FakeStore()
        await run(world, store)
        archive = parse_archive(next(iter(store.objects.values())))
        async with world.sessions() as db:
            await db.execute(delete(User).where(User.id == other))
            await db.commit()
            plan = await plan_restore(db, archive)
            assert not plan.ok and "user" in plan.conflicts[0]
            async with world.sessions() as db2:
                email = (await db2.execute(select(User.email).where(User.id == world.user_id))).scalar_one()
            plan = await plan_restore(db, archive, user_email=email)
            assert plan.ok
            await apply_restore(db, archive, plan)
            await db.commit()
        async with world.sessions() as db:
            assert (await db.get(Chat, chat_id)).user_id == world.user_id


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


class TestFailures:
    async def test_upload_failure_deletes_nothing(self, world):
        chat_id = await world.chat(idle_days=100)
        result = await run(world, FakeStore(fail=True))
        assert result["status"] == "completed"
        assert result["counts"]["failed"] == {"upload": 1}
        assert result["counts"]["deleted"] == 0
        assert await world.exists(chat_id)
        assert await world.count(Message, Message.chat_id == chat_id) == 2

    async def test_delete_failure_after_upload_is_retried_next_run(self, world, monkeypatch):
        chat_id = await world.chat(idle_days=100)
        real = job.delete_chat
        calls = {"n": 0}

        async def flaky(db, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("connection reset")
            return await real(db, *args, **kwargs)

        monkeypatch.setattr(job, "delete_chat", flaky)
        store = FakeStore()
        first = await run(world, store)
        assert first["counts"]["failed"] == {"delete": 1}
        assert first["counts"]["archived"] == 1
        assert await world.exists(chat_id)

        second = await run(world, store)
        assert second["counts"]["deleted"] == 1
        assert not await world.exists(chat_id)
        # Uploaded again to the same key, then deleted.
        assert len(store.puts) == 2 and store.puts[0] == store.puts[1]

    async def test_chat_written_to_during_upload_is_kept(self, world):
        chat_id = await world.chat(idle_days=100)

        async def new_message(key):
            async with world.sessions() as db:
                db.add(Message(chat_id=chat_id, role="user", content="I'm back"))
                await db.commit()

        result = await run(world, FakeStore(on_put=new_message))
        assert result["counts"]["skipped"] == {"changed": 1}
        assert await world.exists(chat_id)
        assert await world.count(Message, Message.chat_id == chat_id) == 3

    async def test_errors_never_carry_content(self, world, caplog):
        chat_id = await world.chat(idle_days=100)
        async with world.sessions() as db:
            await db.execute(
                Message.__table__.update().where(Message.chat_id == chat_id).values(content="SECRET-CONTENT-123")
            )
            await db.commit()
        with caplog.at_level("DEBUG", logger="app.services.retention"):
            await run(world, FakeStore(fail=True))
            await run(world, FakeStore())
        assert "SECRET-CONTENT-123" not in caplog.text
        assert "retention test chat" not in caplog.text


# ---------------------------------------------------------------------------
# Batches, budgets, concurrency
# ---------------------------------------------------------------------------


class TestBatchesAndConcurrency:
    async def test_small_batches_cover_everything(self, world, monkeypatch):
        monkeypatch.setattr(settings, "chat_retention_batch_size", 2)
        chats = [await world.chat(idle_days=100 + i) for i in range(5)]
        blocked = await world.chat(idle_days=300)
        await world.add(PendingCompletion(chat_id=blocked, user_id=world.user_id, channel_id="c",
                                          pending={}, results={}, remaining=1, conversation_context={}))
        result = await run(world, FakeStore())
        assert result["counts"]["candidates"] == 6
        assert result["counts"]["deleted"] == 5
        assert [await world.exists(c) for c in chats] == [False] * 5
        assert await world.exists(blocked)

    async def test_budget_ends_partial_and_the_next_run_continues(self, world, monkeypatch):
        monkeypatch.setattr(settings, "chat_retention_max_chats_per_run", 2)
        chats = [await world.chat(idle_days=100 + i) for i in range(3)]
        first = await run(world, FakeStore())
        assert first["status"] == "partial" and first["counts"]["deleted"] == 2
        assert first["counts"]["more_remaining"] is True
        second = await run(world, FakeStore())
        assert second["status"] == "completed" and second["counts"]["deleted"] == 1
        assert [await world.exists(c) for c in chats] == [False] * 3

    async def test_two_runners_never_process_the_same_chat(self, world):
        chats = [await world.chat(idle_days=100 + i) for i in range(8)]
        store = FakeStore(delay=0.05)  # one store: every PutObject either runner makes
        a, b = await asyncio.gather(run(world, store), run(world, store))
        assert sorted(store.puts) == sorted(set(store.puts)), "a chat was uploaded twice"
        assert len(store.puts) == 8
        assert a["counts"]["deleted"] + b["counts"]["deleted"] == 8
        # Both did work, and each stepped over what the other had claimed.
        assert a["counts"]["deleted"] and b["counts"]["deleted"], (a["counts"], b["counts"])
        claimed = a["counts"]["skipped"].get("claimed", 0) + b["counts"]["skipped"].get("claimed", 0)
        assert claimed, (a["counts"], b["counts"])
        assert [await world.exists(c) for c in chats] == [False] * 8

    async def test_a_claimed_chat_is_skipped(self, world):
        chat_id = await world.chat(idle_days=100)
        async with job.Claims(world.engine) as other:
            assert await other.try_claim(chat_id)
            result = await run(world, FakeStore())
            assert result["counts"]["skipped"] == {"claimed": 1}
        assert await world.exists(chat_id)


# ---------------------------------------------------------------------------
# Dry run, status, schedule
# ---------------------------------------------------------------------------


class TestDryRunAndStatus:
    async def test_preview_reports_per_agent_and_changes_nothing(self, world):
        agent = await world.agent(retention_days=5)
        a1 = await world.chat(agent, idle_days=10, messages=3)
        a2 = await world.chat(agent, idle_days=10, messages=1)
        parked = await world.chat(agent, idle_days=10)
        await world.add(PendingCompletion(chat_id=parked, user_id=world.user_id, channel_id="c",
                                          pending={}, results={}, remaining=1, conversation_context={}))
        before = (await world.count(Chat), await world.count(Message), await world.count(ChatRetentionRun))
        async with world.sessions() as db:
            report = await job.preview(db)
        after = (await world.count(Chat), await world.count(Message), await world.count(ChatRetentionRun))
        assert before == after
        entry = next(e for e in report["agents"] if e["agent"] == f"rettest/{agent.name}")
        assert entry == {
            "agent": f"rettest/{agent.name}", "retention_days": 5, "source": "agent",
            "chats": 2, "messages": 4, "blocked": {"parked": 1},
        }
        assert report["refusal"] is not None  # archive action, no destination
        assert await world.exists(a1) and await world.exists(a2)

    async def test_cli_dry_run_reports_and_changes_nothing(self, world):
        import sys

        chat_id = await world.chat(idle_days=100)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.cli", "retention", "--dry-run",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "CHAT_RETENTION_DAYS": "30"},
        )
        out, err = await proc.communicate()
        assert proc.returncode == 0, err.decode()
        report = json.loads(out)
        assert report["dry_run"] is True and report["totals"]["chats"] >= 1
        assert await world.exists(chat_id)

    async def test_status_shows_the_last_run(self, world):
        await world.chat(idle_days=100)
        await run(world, FakeStore())
        async with world.sessions() as db:
            status = await job.status(db)
        assert status["last_run"]["status"] == "completed"
        assert status["last_run"]["counts"]["deleted"] == 1
        assert status["next_run"] is not None
        assert status["in_progress"] is None

    async def test_a_queued_run_no_worker_took_stops_counting_as_in_progress(self, world):
        await world.add(ChatRetentionRun(trigger="api", status="queued", created_at=NOW - timedelta(minutes=20)))
        async with world.sessions() as db:
            assert (await job.status(db))["in_progress"] is None
        await world.add(ChatRetentionRun(trigger="api", status="queued", created_at=NOW))
        async with world.sessions() as db:
            assert (await job.status(db))["in_progress"]["status"] == "queued"

    async def test_scheduled_tick_waits_for_the_schedule(self, world, monkeypatch):
        old = await world.chat(idle_days=100)
        # Fires once a year, long ago this year: a deployment that never ran waits.
        monkeypatch.setattr(settings, "chat_retention_schedule", "0 0 1 1 *")
        monkeypatch.setattr(job, "run_retention", _fail_if_called)
        assert await job.scheduled_tick(engine=world.engine, now=datetime(2026, 6, 1, tzinfo=UTC)) is None
        assert await world.exists(old)


async def _fail_if_called(**kwargs):
    raise AssertionError("no run was due")
