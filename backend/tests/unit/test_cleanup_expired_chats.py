"""The expired-chat cleanup job against a migrated database.

Migration 54056f649874 left `messages`, `pending_tool_approvals`,
`pending_completions` and `executions` referencing `chats` with NO ACTION,
so a plain `DELETE FROM chats` fails on any chat with messages. These use
real connections and real commits (the job opens its own session and
commits per batch), and remove what they created.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.chat import Chat, Message
from app.models.chat_wakeup import TIMER, ChatWakeup
from app.models.execution import Execution, ExecutionStatus, TriggerType
from app.models.file import Collection, File, FileVersion
from app.models.llm_usage import LLMUsage
from app.models.mcp_server import McpServer
from app.models.mcp_task import McpTask
from app.models.pending_approval import PendingToolApproval
from app.models.pending_completion import PendingCompletion
from app.models.tool_call_result import ToolCallResult
from app.models.user import User
from app.scheduler.jobs import cleanup_expired_chats as cleanup_module
from tests.conftest import _test_db_url

NOW = datetime.now(UTC)


class World:
    def __init__(self, engine):
        self.sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        self.user_id: uuid.UUID | None = None
        self.server_ids: list[uuid.UUID] = []

    async def setup(self):
        async with self.sessions() as db:
            user = User(email=f"expire-{uuid.uuid4().hex[:10]}@example.com")
            db.add(user)
            await db.commit()
            self.user_id = user.id

    async def add(self, *objs):
        async with self.sessions() as db:
            for obj in objs:
                db.add(obj)
                await db.flush()
            await db.commit()

    async def chat(self, *, expires_in: timedelta | None, messages: int = 2) -> uuid.UUID:
        async with self.sessions() as db:
            chat = Chat(user_id=self.user_id, title="expiring chat",
                        expires_at=(NOW + expires_in) if expires_in is not None else None)
            db.add(chat)
            await db.flush()
            for i in range(messages):
                db.add(Message(chat_id=chat.id, role="user" if i % 2 == 0 else "assistant",
                               content=f"message {i}"))
            await db.commit()
            return chat.id

    async def chats(self, n: int, *, messages: int = 1) -> list[uuid.UUID]:
        """Many expired chats in one transaction."""
        async with self.sessions() as db:
            chats = [Chat(user_id=self.user_id, title="bulk", expires_at=NOW - timedelta(hours=1))
                     for _ in range(n)]
            db.add_all(chats)
            await db.flush()
            db.add_all([Message(chat_id=c.id, role="user", content="hi") for c in chats for _ in range(messages)])
            await db.commit()
            return [c.id for c in chats]

    async def first_message(self, chat_id) -> uuid.UUID:
        async with self.sessions() as db:
            return (await db.execute(select(Message.id).where(Message.chat_id == chat_id).limit(1))).scalar_one()

    async def exists(self, chat_id) -> bool:
        async with self.sessions() as db:
            return (await db.execute(select(Chat.id).where(Chat.id == chat_id))).scalar_one_or_none() is not None

    async def count(self, model, *where) -> int:
        async with self.sessions() as db:
            return (await db.execute(select(func.count()).select_from(model).where(*where))).scalar()

    async def cleanup(self):
        async with self.sessions() as db:
            chat_ids = (await db.execute(select(Chat.id).where(Chat.user_id == self.user_id))).scalars().all()
            if chat_ids:
                for model in (PendingToolApproval, PendingCompletion, ToolCallResult, ChatWakeup, McpTask):
                    await db.execute(delete(model).where(model.chat_id.in_(chat_ids)))
                await db.execute(delete(Collection).where(Collection.name.in_([str(c) for c in chat_ids])))
                await db.execute(delete(Message).where(Message.chat_id.in_(chat_ids)))
            await db.execute(delete(Execution).where(Execution.user_id == self.user_id))
            if chat_ids:
                await db.execute(delete(Chat).where(Chat.id.in_(chat_ids)))
            await db.execute(delete(LLMUsage).where(LLMUsage.user_id == self.user_id))
            await db.execute(delete(Collection).where(Collection.user_id == self.user_id))
            await db.execute(delete(McpServer).where(McpServer.id.in_(self.server_ids)))
            await db.execute(delete(User).where(User.id == self.user_id))
            await db.commit()


@pytest_asyncio.fixture
async def world(monkeypatch):
    engine = create_async_engine(_test_db_url)
    w = World(engine)
    await w.setup()
    # The job's own sessions, on this test's event loop.
    monkeypatch.setattr(cleanup_module, "AsyncSessionLocal", w.sessions)
    try:
        yield w
    finally:
        await w.cleanup()
        await engine.dispose()


async def test_expired_chat_with_messages_goes(world):
    chat_id = await world.chat(expires_in=-timedelta(minutes=5), messages=2)
    await cleanup_module.cleanup_expired_chats()
    assert not await world.exists(chat_id)
    assert await world.count(Message, Message.chat_id == chat_id) == 0


async def test_expired_chat_and_everything_owned_goes_executions_unlinked(world, monkeypatch, tmp_path):
    import app.services.file_storage as file_storage
    from app.services.tool_result_store import ensure_partitions

    monkeypatch.setattr(file_storage, "_storage", file_storage.LocalFileStorage(str(tmp_path)))
    chat_id = await world.chat(expires_in=-timedelta(minutes=5), messages=3)
    kept = await world.chat(expires_in=timedelta(days=1))
    forever = await world.chat(expires_in=None)
    message_id = await world.first_message(chat_id)
    async with world.sessions() as db:
        await ensure_partitions(db)
    server = McpServer(user_id=world.user_id, namespace="expiretest", name=f"s{uuid.uuid4().hex[:8]}",
                       url="https://mcp.example.test")
    await world.add(server)
    world.server_ids.append(server.id)
    workbench = Collection(namespace="_chat", name=str(chat_id), kind="workbench", user_id=world.user_id)
    await world.add(workbench)
    file = File(collection_id=workbench.id, name="notes.md", user_id=world.user_id, content_type="text/markdown")
    await world.add(file)
    storage_path = f"_chat/{file.id}/v1"
    await file_storage.get_storage().save(storage_path, b"workbench bytes")
    execution_id = f"exec-{uuid.uuid4().hex}"
    usage_id = uuid.uuid4()
    await world.add(
        FileVersion(file_id=file.id, version_number=1, storage_path=storage_path, size_bytes=15,
                    hash_sha256="ab" * 32, uploaded_by=world.user_id),
        # Unanswered and still parked: an expired chat goes regardless.
        PendingToolApproval(chat_id=chat_id, message_id=message_id, user_id=world.user_id,
                            tool_call_id="call_x", function_namespace="d", function_name="f", arguments={},
                            all_tool_calls=[], conversation_context={}),
        PendingCompletion(chat_id=chat_id, user_id=world.user_id, channel_id="c", pending={"t": {}},
                          results={}, remaining=1, conversation_context={}),
        ChatWakeup(chat_id=chat_id, user_id=world.user_id, kind=TIMER, due_at=NOW + timedelta(days=1)),
        McpTask(chat_id=chat_id, user_id=world.user_id, mcp_server_id=server.id, server_task_id="st",
                tool_name="t", model_tool_name="t", status="working"),
        Execution(user_id=world.user_id, execution_id=execution_id, function_name="f",
                  trigger_type=TriggerType.AGENT, trigger_id="x", chat_id=chat_id,
                  status=ExecutionStatus.COMPLETED, input_data={}, started_at=NOW),
        ToolCallResult(tool_call_id="call_x", chat_id=chat_id, user_id=world.user_id, tool_name="f",
                       result={"v": 1}, source="agent", created_at=NOW, expires_at=NOW + timedelta(days=30)),
        LLMUsage(id=usage_id, user_id=world.user_id, chat_id=chat_id, message_id=message_id,
                 prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )

    await cleanup_module.cleanup_expired_chats()

    assert not await world.exists(chat_id)
    for model in (Message, PendingToolApproval, PendingCompletion, ChatWakeup, McpTask, ToolCallResult):
        assert await world.count(model, model.chat_id == chat_id) == 0, model
    assert await world.count(Collection, Collection.id == workbench.id) == 0
    assert not (tmp_path / storage_path).exists()
    async with world.sessions() as db:
        execution = (await db.execute(select(Execution).where(Execution.execution_id == execution_id))).scalar_one()
        assert execution.chat_id is None
        usage = await db.get(LLMUsage, usage_id)
        assert usage is not None and usage.chat_id == chat_id
    # Not expired, or no expiry: untouched, messages and all.
    for chat in (kept, forever):
        assert await world.exists(chat)
        assert await world.count(Message, Message.chat_id == chat) == 2


async def test_many_expired_chats_go_in_batches(world, monkeypatch):
    monkeypatch.setattr(cleanup_module, "BATCH_SIZE", 40)
    chat_ids = await world.chats(130, messages=2)
    commits = 0
    real_sessions = cleanup_module.AsyncSessionLocal

    class Counting:
        def __call__(self):
            session = real_sessions()
            real_commit = session.commit

            async def commit():
                nonlocal commits
                commits += 1
                await real_commit()

            session.commit = commit
            return session

    monkeypatch.setattr(cleanup_module, "AsyncSessionLocal", Counting())
    await cleanup_module.cleanup_expired_chats()
    assert await world.count(Chat, Chat.id.in_(chat_ids)) == 0
    assert await world.count(Message, Message.chat_id.in_(chat_ids)) == 0
    assert commits == 4  # 130 chats, 40 a transaction


async def test_a_chat_that_fails_to_delete_does_not_block_the_rest(world, monkeypatch):
    """One chat whose rows can't go (an unforeseen reference, say) is kept
    and logged; its batch is retried chat by chat, and the run ends."""
    monkeypatch.setattr(cleanup_module, "BATCH_SIZE", 3)
    chat_ids = await world.chats(7)
    poisoned = chat_ids[1]
    real = cleanup_module.delete_chats_and_rows

    async def failing(db, ids):
        if poisoned in ids:
            raise RuntimeError("still referenced")
        return await real(db, ids)

    monkeypatch.setattr(cleanup_module, "delete_chats_and_rows", failing)
    await cleanup_module.cleanup_expired_chats()
    assert await world.exists(poisoned)
    assert await world.count(Chat, Chat.id.in_(chat_ids)) == 1


async def test_a_locked_chat_is_skipped_not_waited_for(world):
    """A chat whose row another transaction holds (a turn writing a message
    takes a key-share lock on it) is left for the next run; the rest go."""
    busy = await world.chat(expires_in=-timedelta(minutes=1))
    free = await world.chat(expires_in=-timedelta(minutes=1))
    async with world.sessions() as holder:
        await holder.execute(select(Chat.id).where(Chat.id == busy).with_for_update(key_share=True))
        await cleanup_module.cleanup_expired_chats()
        assert await world.exists(busy)
        assert not await world.exists(free)
        await holder.rollback()
    await cleanup_module.cleanup_expired_chats()
    assert not await world.exists(busy)
