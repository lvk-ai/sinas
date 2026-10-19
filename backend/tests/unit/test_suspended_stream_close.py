"""A stream whose round is parked on ask_user alone ends with `done`
(status "suspended") instead of idling until the relay times out: the
answer resumes on a fresh channel, so nothing more arrives on this one.
A round also waiting on a background run or sub-agent resumes on this same
channel, so its stream stays open."""
import asyncio
import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.runtime.endpoints.chats import round_waits_only_on_user
from app.models.chat import Chat
from app.models.user import User
from app.services.stream_relay import stream_relay
from tests.conftest import auth_headers


@pytest_asyncio.fixture
async def chat(db: AsyncSession, test_user: User) -> Chat:
    c = Chat(user_id=test_user.id, title="suspended stream", agent_namespace="e2e", agent_name="a")
    db.add(c)
    await db.flush()
    await db.refresh(c)
    return c


def _fake_subscribe(events):
    async def subscribe(channel_id, last_id="0", timeout=None):
        for e in events:
            yield e
        await asyncio.sleep(3600)  # an open channel with nothing more to say
    return subscribe


async def _read_stream(client, chat, user):
    seen = []
    async with client.stream("GET", f"/chats/{chat.id}/stream/chan-1", headers=auth_headers(user)) as resp:
        assert resp.status_code == 200
        event = None
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event != "ping":
                seen.append((event, line[5:].strip()))
                if event in ("done", "error"):
                    break
    return seen


def _in(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def test_only_a_fully_asked_round_waits_on_the_user():
    assert round_waits_only_on_user({"tool_call_ids": ["a", "b"]}, {"a": None, "b": _in(86400)})
    assert not round_waits_only_on_user({"tool_call_ids": ["a", "bg"]}, {"a": None})
    assert not round_waits_only_on_user({"tool_call_ids": []}, {})


def test_a_question_that_can_time_out_while_the_stream_is_open_keeps_it_open():
    """Its timeout resumes the round on this channel: the reply should be
    seen live, not lost to a closed stream."""
    assert not round_waits_only_on_user({"tool_call_ids": ["a"]}, {"a": _in(30)})
    assert not round_waits_only_on_user({"tool_call_ids": ["a", "b"]}, {"a": None, "b": _in(60)})


@pytest.mark.asyncio
async def test_round_parked_on_ask_user_ends_the_stream(client, chat, test_user, monkeypatch):
    monkeypatch.setattr(stream_relay, "subscribe", _fake_subscribe([
        {"type": "tool_start", "tool_call_id": "a", "name": "ask_user"},
        {"type": "input_required", "tool_call_id": "a", "question": "Which?"},
        {"type": "round_suspended", "pending_completion_id": "p", "tool_call_ids": ["a"]},
    ]))
    seen = await asyncio.wait_for(_read_stream(client, chat, test_user), timeout=10)
    assert [e for e, _ in seen] == ["tool_start", "input_required", "round_suspended", "done"]
    assert json.loads(seen[-1][1]) == {"status": "suspended"}


@pytest.mark.asyncio
async def test_round_also_waiting_on_a_background_run_stays_open(client, chat, test_user, monkeypatch):
    monkeypatch.setattr(stream_relay, "subscribe", _fake_subscribe([
        {"type": "input_required", "tool_call_id": "a", "question": "Which?"},
        {"type": "round_suspended", "pending_completion_id": "p", "tool_call_ids": ["a", "bg"]},
    ]))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_read_stream(client, chat, test_user), timeout=2)


@pytest.mark.asyncio
async def test_a_question_expiring_soon_keeps_the_stream_open(client, chat, test_user, monkeypatch):
    monkeypatch.setattr(stream_relay, "subscribe", _fake_subscribe([
        {"type": "input_required", "tool_call_id": "a", "question": "Which?", "expires_at": _in(30)},
        {"type": "round_suspended", "pending_completion_id": "p", "tool_call_ids": ["a"]},
    ]))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_read_stream(client, chat, test_user), timeout=2)


@pytest.mark.asyncio
async def test_a_stream_follows_its_round_to_the_channel_it_resumed_on(client, chat, test_user, monkeypatch):
    """Kept open for a background run, the round's question is answered
    meanwhile and the round resumes on the answer's fresh channel: the
    stream moves there instead of idling where nothing more will arrive."""
    channels = {
        "chan-1": [
            {"type": "input_required", "tool_call_id": "a", "question": "Which?"},
            {"type": "round_suspended", "pending_completion_id": "p", "tool_call_ids": ["a", "bg"]},
            {"type": "tool_end", "tool_call_id": "bg"},
            {"type": "round_moved", "channel_id": "chan-2"},
        ],
        "chan-2": [{"content": "resumed"}, {"type": "done", "status": "completed"}],
    }

    async def subscribe(channel_id, last_id="0", timeout=None):
        for e in channels[channel_id]:
            yield e
        await asyncio.sleep(3600)

    monkeypatch.setattr(stream_relay, "subscribe", subscribe)
    seen = await asyncio.wait_for(_read_stream(client, chat, test_user), timeout=10)
    assert [e for e, _ in seen][-2:] == ["message", "done"]
    assert json.loads(seen[-2][1])["content"] == "resumed"
    assert "round_moved" not in "".join(d for _, d in seen)
