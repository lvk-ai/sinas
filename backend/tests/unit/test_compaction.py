"""Compaction: summarize-and-continue instead of the 100-message cliff.

Covers: the incremental refresh (each message summarized at most once, the
prior summary folded into the next), the injection of the stored summary
into the built history when windowing applies, and the schedule trigger
when the summary lags the window.
"""
import json
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.chat import Chat, Message
from app.models.user import User
from app.services import compaction
from app.services.conversation_history import build_conversation_history
from app.services.skill_tools import SkillToolConverter


@pytest_asyncio.fixture
async def long_chat(db: AsyncSession, test_user: User) -> Chat:
    chat = Chat(user_id=test_user.id, title="long chat")
    db.add(chat)
    await db.flush()
    for i in range(10):
        role = "user" if i % 2 == 0 else "assistant"
        db.add(Message(chat_id=chat.id, role=role, content=f"message {i}"))
        await db.flush()  # distinct created_at ordering
    await db.refresh(chat)
    return chat


class TestRunCompaction:
    @pytest.mark.asyncio
    async def test_incremental_refresh(self, db, long_chat, monkeypatch):
        monkeypatch.setattr(settings, "max_history_messages", 6)

        seen: list[tuple[str, int]] = []

        async def fake_summarize(_db, _chat, prior_summary, delta):
            seen.append((prior_summary, len(delta)))
            return f"summary covering {len(delta)} more"

        monkeypatch.setattr(compaction, "_summarize", fake_summarize)

        # First run: 10 messages, window 6 → summarize the first 4.
        result = await compaction.run_compaction(str(long_chat.id), db=db)
        assert result["covered_count"] == 4
        assert seen == [("", 4)]

        # No growth → no re-summarization.
        again = await compaction.run_compaction(str(long_chat.id), db=db)
        assert again["covered_count"] == 4
        assert len(seen) == 1

        # Chat grows by 3 → only the 3-message delta is folded in, with the
        # prior summary as input.
        for i in range(3):
            db.add(Message(chat_id=long_chat.id, role="user", content=f"more {i}"))
            await db.flush()
        result = await compaction.run_compaction(str(long_chat.id), db=db)
        assert result["covered_count"] == 7
        assert seen[-1] == ("summary covering 4 more", 3)

    @pytest.mark.asyncio
    async def test_short_chat_is_untouched(self, db, long_chat, monkeypatch):
        monkeypatch.setattr(settings, "max_history_messages", 100)
        result = await compaction.run_compaction(str(long_chat.id), db=db)
        assert result is None
        assert compaction.get_compaction(long_chat) is None

    @pytest.mark.asyncio
    async def test_failed_summarize_keeps_previous_state(self, db, long_chat, monkeypatch):
        monkeypatch.setattr(settings, "max_history_messages", 6)

        async def broken(_db, _chat, prior, delta):
            return None

        monkeypatch.setattr(compaction, "_summarize", broken)
        result = await compaction.run_compaction(str(long_chat.id), db=db)
        assert result is None
        assert compaction.get_compaction(long_chat) is None


class TestHistoryInjection:
    @pytest.mark.asyncio
    async def test_summary_injected_when_windowing(self, db, long_chat, monkeypatch):
        monkeypatch.setattr(settings, "max_history_messages", 6)
        long_chat.chat_metadata = {
            "compaction": {"summary": "- user likes terse answers", "covered_count": 4}
        }
        await db.flush()

        scheduled: list[str] = []
        monkeypatch.setattr(
            compaction, "maybe_schedule_compaction", lambda chat_id: scheduled.append(chat_id)
        )

        messages = await build_conversation_history(db, long_chat, SkillToolConverter())
        system_msgs = [m for m in messages if m["role"] == "system"]
        assert any("user likes terse answers" in m["content"] for m in system_msgs)
        # summary covers exactly the dropped prefix — no refresh needed
        assert scheduled == []
        # windowed history follows the summary
        assert sum(1 for m in messages if m["role"] in ("user", "assistant")) == 6

    @pytest.mark.asyncio
    async def test_lagging_summary_triggers_refresh(self, db, long_chat, monkeypatch):
        monkeypatch.setattr(settings, "max_history_messages", 4)  # dropped = 6 > covered 4
        long_chat.chat_metadata = {
            "compaction": {"summary": "- old summary", "covered_count": 4}
        }
        await db.flush()

        scheduled: list[str] = []
        monkeypatch.setattr(
            compaction, "maybe_schedule_compaction", lambda chat_id: scheduled.append(chat_id)
        )
        await build_conversation_history(db, long_chat, SkillToolConverter())
        assert scheduled == [str(long_chat.id)]

    @pytest.mark.asyncio
    async def test_no_summary_no_injection_but_scheduled(self, db, long_chat, monkeypatch):
        monkeypatch.setattr(settings, "max_history_messages", 6)
        scheduled: list[str] = []
        monkeypatch.setattr(
            compaction, "maybe_schedule_compaction", lambda chat_id: scheduled.append(chat_id)
        )
        messages = await build_conversation_history(db, long_chat, SkillToolConverter())
        assert not any("summarized" in (m.get("content") or "") for m in messages if m["role"] == "system")
        assert scheduled == [str(long_chat.id)]

    @pytest.mark.asyncio
    async def test_compaction_disabled_restores_pure_windowing(self, db, long_chat, monkeypatch):
        monkeypatch.setattr(settings, "max_history_messages", 6)
        monkeypatch.setattr(settings, "compaction_enabled", False)
        long_chat.chat_metadata = {
            "compaction": {"summary": "- should not appear", "covered_count": 4}
        }
        await db.flush()
        messages = await build_conversation_history(db, long_chat, SkillToolConverter())
        assert not any(
            "should not appear" in (m.get("content") or "") for m in messages
        )


class TestFollowupUsesWindowing:
    """Issue #225: the tool-round boundary rebuild must apply the same
    windowing and compaction as a fresh send, or a long autonomous turn
    outgrows the context a fresh send on the same chat would never see."""

    class _FakeProvider:
        def __init__(self):
            self.seen: list[list[dict]] = []

        async def stream(self, messages, model, tools, temperature, max_tokens):
            self.seen.append(messages)
            yield {"content": "ok", "tool_calls": None, "finish_reason": "stop"}

    async def _run_followup(self, db, chat, user, monkeypatch):
        from app.services import message_service as ms

        fake = self._FakeProvider()

        async def fake_create_provider(*_a, **_k):
            return fake

        monkeypatch.setattr(ms, "create_provider", fake_create_provider)
        service = ms.MessageService(db)
        async for _ in service._stream_followup_after_tools(
            chat_id=str(chat.id), user_id=str(user.id), user_token="",
            provider=None, model=None, temperature=0.7, max_tokens=None, tools=[],
        ):
            pass
        assert len(fake.seen) == 1
        return fake.seen[0]

    @pytest.mark.asyncio
    async def test_followup_is_windowed(self, db, long_chat, test_user, monkeypatch):
        monkeypatch.setattr(settings, "max_history_messages", 4)
        monkeypatch.setattr(settings, "compaction_enabled", False)
        sent = await self._run_followup(db, long_chat, test_user, monkeypatch)
        assert [m["content"] for m in sent] == [f"message {i}" for i in range(6, 10)]

    @pytest.mark.asyncio
    async def test_followup_injects_compaction_summary(self, db, long_chat, test_user, monkeypatch):
        monkeypatch.setattr(settings, "max_history_messages", 4)
        monkeypatch.setattr(settings, "compaction_enabled", True)
        monkeypatch.setattr(compaction, "maybe_schedule_compaction", lambda _cid: None)
        long_chat.chat_metadata = {
            "compaction": {"summary": "earlier: six messages", "covered_count": 6}
        }
        await db.flush()
        sent = await self._run_followup(db, long_chat, test_user, monkeypatch)
        assert "earlier: six messages" in sent[0]["content"]
        assert [m["content"] for m in sent[1:]] == [f"message {i}" for i in range(6, 10)]

    @pytest.mark.asyncio
    async def test_followup_keeps_tool_pairs_and_compacts_old_results(
        self, db, test_user, monkeypatch
    ):
        chat = Chat(user_id=test_user.id, title="tool-heavy turn")
        db.add(chat)
        await db.flush()
        db.add(Message(chat_id=chat.id, role="user", content="go"))
        await db.flush()
        for i in range(6):
            db.add(Message(chat_id=chat.id, role="assistant", content=None, tool_calls=[
                {"id": f"c{i}", "type": "function", "function": {"name": "t", "arguments": "{}"}}
            ]))
            await db.flush()
            db.add(Message(chat_id=chat.id, role="tool", name="t", tool_call_id=f"c{i}",
                           content=json.dumps({"big": "x" * 50, "i": i})))
            await db.flush()
        monkeypatch.setattr(settings, "max_history_messages", 5)
        monkeypatch.setattr(settings, "compaction_enabled", False)
        monkeypatch.setattr(settings, "tool_result_max_inline", 1)
        sent = await self._run_followup(db, chat, test_user, monkeypatch)
        # Window boundary never splits a tool call from its result, and no
        # orphaned tool result survives.
        ids_called = {tc["id"] for m in sent if m.get("tool_calls") for tc in m["tool_calls"]}
        ids_answered = {m["tool_call_id"] for m in sent if m["role"] == "tool"}
        assert ids_called == ids_answered
        # Only the newest result stays inline; older ones are pointers.
        tool_msgs = [m for m in sent if m["role"] == "tool"]
        assert tool_msgs[-1]["content"].startswith("{")
        assert all("retrieve_tool_result" in m["content"] for m in tool_msgs[:-1])

    @pytest.mark.asyncio
    async def test_followup_pins_the_turn_opener(self, db, test_user, monkeypatch):
        """A long turn's opening user message is the oldest of its messages
        and would be the first to fall out of the window; it is pinned so the
        model always sees what it was asked."""
        chat = Chat(user_id=test_user.id, title="long turn")
        db.add(chat)
        await db.flush()
        # previous turn, finished
        db.add(Message(chat_id=chat.id, role="user", content="old ask"))
        await db.flush()
        db.add(Message(chat_id=chat.id, role="assistant", content="old reply"))
        await db.flush()
        # current turn: opener + many tool rounds
        db.add(Message(chat_id=chat.id, role="user", content="THE TASK"))
        await db.flush()
        for i in range(5):
            db.add(Message(chat_id=chat.id, role="assistant", content=None, tool_calls=[
                {"id": f"r{i}", "type": "function", "function": {"name": "t", "arguments": "{}"}}
            ]))
            await db.flush()
            db.add(Message(chat_id=chat.id, role="tool", name="t", tool_call_id=f"r{i}", content="{}"))
            await db.flush()
        monkeypatch.setattr(settings, "max_history_messages", 4)
        monkeypatch.setattr(settings, "compaction_enabled", False)
        sent = await self._run_followup(db, chat, test_user, monkeypatch)
        users = [m["content"] for m in sent if m["role"] == "user"]
        assert users == ["THE TASK"]  # pinned; the previous turn's ask stays out
        assert sent[0]["role"] == "user"  # ahead of the windowed tail
        assert [m["tool_call_id"] for m in sent if m["role"] == "tool"] == ["r3", "r4"]

    @pytest.mark.asyncio
    async def test_window_cutting_a_batch_keeps_its_real_results(self, db, test_user, monkeypatch):
        """When the window starts inside a batch of tool results, the whole
        exchange comes back: the call and the batch's earlier results, not
        synthetic 'interrupted' errors for calls that completed."""
        chat = Chat(user_id=test_user.id, title="batch")
        db.add(chat)
        await db.flush()
        db.add(Message(chat_id=chat.id, role="user", content="go"))
        await db.flush()
        db.add(Message(chat_id=chat.id, role="assistant", content=None, tool_calls=[
            {"id": f"b{i}", "type": "function", "function": {"name": "t", "arguments": "{}"}} for i in range(3)
        ]))
        await db.flush()
        for i in range(3):
            db.add(Message(chat_id=chat.id, role="tool", name="t", tool_call_id=f"b{i}", content=json.dumps({"ok": i})))
            await db.flush()
        db.add(Message(chat_id=chat.id, role="assistant", content="then"))
        await db.flush()
        monkeypatch.setattr(settings, "max_history_messages", 2)  # window: [tool b2, assistant]
        monkeypatch.setattr(settings, "compaction_enabled", False)
        sent = await self._run_followup(db, chat, test_user, monkeypatch)
        tools = [m for m in sent if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tools] == ["b0", "b1", "b2"]
        assert all("interrupted" not in m["content"] for m in tools)


    @pytest.mark.asyncio
    async def test_every_steering_message_of_the_turn_is_pinned(self, db, test_user, monkeypatch):
        chat = Chat(user_id=test_user.id, title="steered")
        db.add(chat)
        await db.flush()
        db.add(Message(chat_id=chat.id, role="user", content="THE TASK"))
        await db.flush()
        for i in range(4):
            db.add(Message(chat_id=chat.id, role="assistant", content=None, tool_calls=[
                {"id": f"s{i}", "type": "function", "function": {"name": "t", "arguments": "{}"}}
            ]))
            await db.flush()
            db.add(Message(chat_id=chat.id, role="user", content=f"steer {i}"))  # injected mid-round
            await db.flush()
            db.add(Message(chat_id=chat.id, role="tool", name="t", tool_call_id=f"s{i}", content="{}"))
            await db.flush()
        monkeypatch.setattr(settings, "max_history_messages", 3)
        monkeypatch.setattr(settings, "compaction_enabled", False)
        sent = await self._run_followup(db, chat, test_user, monkeypatch)
        users = [m["content"] for m in sent if m["role"] == "user"]
        assert users == ["THE TASK", "steer 0", "steer 1", "steer 2", "steer 3"]
