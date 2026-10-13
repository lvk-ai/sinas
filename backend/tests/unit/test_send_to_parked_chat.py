"""A message sent to a chat whose round is parked is injected, not run.

A parked round (waiting on ask_user, a background run, a sub-agent or an
approval) holds no chat lock, but it owns the chat: a new turn would
"repair" its open tool calls with synthetic errors and race its resume.
The send paths used to start one anyway, so a steering message sent while
a round waited on background work ran as a separate turn whose reply never
reached the open stream. Now it is injected and the resumed round reads it.
"""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Chat, Message
from app.models.pending_completion import PendingCompletion
from app.models.user import User
from app.services import chat_steering
from app.services.message_service import MessageService


@pytest_asyncio.fixture
async def parked_chat(db: AsyncSession, test_user: User) -> Chat:
    chat = Chat(user_id=test_user.id, title="parked")
    db.add(chat)
    await db.flush()
    db.add(PendingCompletion(
        chat_id=chat.id, user_id=test_user.id, channel_id="chan", pending={"bg": {"completer": "background_job"}},
        results={}, remaining=1, conversation_context={},
    ))
    await db.flush()
    await db.refresh(chat)
    return chat


async def _no_turn(*args, **kwargs):
    raise AssertionError("a turn was started on a parked chat")
    yield  # pragma: no cover


@pytest.fixture
def no_turns(monkeypatch):
    monkeypatch.setattr(MessageService, "_send_message_locked", _no_turn)
    monkeypatch.setattr(MessageService, "_send_message_stream_locked", _no_turn)


async def _user_messages(db, chat) -> list[str]:
    return list((await db.execute(
        select(Message.content).where(Message.chat_id == chat.id, Message.role == "user")
    )).scalars())


@pytest.mark.asyncio
async def test_send_injects_into_a_parked_chat(db, parked_chat, test_user, no_turns):
    out = await MessageService(db).send_message(str(parked_chat.id), str(test_user.id), "", "steer me")
    assert out.role == "user" and out.content == "steer me"
    assert await _user_messages(db, parked_chat) == ["steer me"]
    assert not await chat_steering.is_chat_locked(str(parked_chat.id))  # the lock was given back


@pytest.mark.asyncio
async def test_streaming_send_injects_into_a_parked_chat(db, parked_chat, test_user, no_turns):
    events = [e async for e in MessageService(db).send_message_stream(
        chat_id=str(parked_chat.id), user_id=str(test_user.id), user_token="", content="steer me",
    )]
    assert [e["type"] for e in events] == ["injected", "done"]
    assert await _user_messages(db, parked_chat) == ["steer me"]
    assert not await chat_steering.is_chat_locked(str(parked_chat.id))


@pytest.mark.asyncio
async def test_a_failing_parked_check_gives_the_lock_back(db, parked_chat, test_user, monkeypatch):
    from app.services import chat_wakeups

    async def boom(*a, **k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(chat_wakeups, "chat_is_parked", boom)
    monkeypatch.setattr(chat_wakeups, "parked_round_channel", boom)
    with pytest.raises(RuntimeError):
        await MessageService(db).send_message(str(parked_chat.id), str(test_user.id), "", "x")
    assert not await chat_steering.is_chat_locked(str(parked_chat.id))
    with pytest.raises(RuntimeError):
        async for _ in MessageService(db).send_message_stream(
            chat_id=str(parked_chat.id), user_id=str(test_user.id), user_token="", content="x",
        ):
            pass
    assert not await chat_steering.is_chat_locked(str(parked_chat.id))


@pytest.mark.asyncio
async def test_the_reconnect_channel_goes_back_to_the_parked_round(db, parked_chat, test_user, no_turns):
    """Queued for streaming, this message's channel was recorded as the
    chat's reconnect channel; it only injects, so the channel must point
    back at the one the parked round resumes on."""
    from app.services.delegation import current_channel_id

    parked_chat.active_channel_id = "this-message"
    await db.flush()
    token = current_channel_id.set("this-message")
    try:
        _ = [e async for e in MessageService(db).send_message_stream(
            chat_id=str(parked_chat.id), user_id=str(test_user.id), user_token="", content="steer",
        )]
    finally:
        current_channel_id.reset(token)
    await db.refresh(parked_chat)
    assert parked_chat.active_channel_id == "chan"  # the pending completion's channel
