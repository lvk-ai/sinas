"""Approving a tool call resolves it only while it is still undecided and
before its deadline — the expiry sweep resolves it the same way, so a late
"approve" can never run a call the sweep already rejected."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.chat import Chat, Message
from app.models.pending_approval import PendingToolApproval
from app.services.queue_service import queue_service
from tests.conftest import auth_headers


async def _pending(db, user, expires_at, asked: int = 1):
    """A round with `asked` calls waiting for approval; returns the chat id
    and the first call id (`_pending_round` returns all of them)."""
    chat_id, call_ids = await _pending_round(db, user, expires_at, asked)
    return chat_id, call_ids[0]


async def _pending_round(db, user, expires_at, asked: int = 1):
    chat = Chat(user_id=user.id, title="t", agent_namespace="ns", agent_name="bot")
    db.add(chat)
    await db.flush()
    anchor = Message(chat_id=chat.id, role="assistant", content="x")
    db.add(anchor)
    await db.flush()
    call_ids = [f"call-{uuid.uuid4().hex[:8]}" for _ in range(asked)]
    round_calls = [{"id": cid, "type": "function", "function": {"name": "purge", "arguments": "{}"}} for cid in call_ids]
    for cid in call_ids:
        db.add(PendingToolApproval(
            chat_id=chat.id, message_id=anchor.id, user_id=user.id, tool_call_id=cid,
            function_namespace="ops", function_name="purge", arguments={}, all_tool_calls=round_calls,
            conversation_context={}, expires_at=expires_at,
        ))
    await db.flush()
    return str(chat.id), call_ids


def _capture(monkeypatch):
    calls = []

    async def fake(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(queue_service, "enqueue_agent_resume", fake)
    return calls


async def test_an_open_approval_resumes(client, db, admin_user, monkeypatch):
    calls = _capture(monkeypatch)
    chat_id, call_id = await _pending(db, admin_user, datetime.now(UTC) + timedelta(hours=1))
    r = await client.post(
        f"/chats/{chat_id}/approve-tool/{call_id}", json={"approved": True}, headers=auth_headers(admin_user)
    )
    assert r.status_code == 202, r.text
    assert len(calls) == 1 and calls[0]["approved"] is True


async def test_an_expired_approval_is_refused(client, db, admin_user, monkeypatch):
    calls = _capture(monkeypatch)
    chat_id, call_id = await _pending(db, admin_user, datetime.now(UTC) - timedelta(minutes=1))
    r = await client.post(
        f"/chats/{chat_id}/approve-tool/{call_id}", json={"approved": True}, headers=auth_headers(admin_user)
    )
    assert r.status_code == 409
    assert calls == []


async def test_a_round_with_several_asks_resumes_once_after_the_last_decision(client, db, admin_user, monkeypatch):
    """Every approval row stores the whole round: resuming on the first
    decision would run the other asked calls unchecked, and the next
    decision would run the round again."""
    calls = _capture(monkeypatch)
    chat_id, (first, second, third) = await _pending_round(db, admin_user, datetime.now(UTC) + timedelta(hours=1), asked=3)
    h = auth_headers(admin_user)

    r = await client.post(f"/chats/{chat_id}/approve-tool/{first}", json={"approved": True}, headers=h)
    assert r.status_code == 202 and r.json()["resume"] == "waiting"
    r = await client.post(f"/chats/{chat_id}/approve-tool/{second}", json={"approved": False}, headers=h)
    assert r.status_code == 202 and r.json()["resume"] == "waiting"
    assert calls == []  # nothing ran yet

    r = await client.post(f"/chats/{chat_id}/approve-tool/{third}", json={"approved": True}, headers=h)
    assert r.status_code == 202 and r.json()["resume"] == "queued"
    assert len(calls) == 1
    assert calls[0]["approved"] is True and calls[0]["rejected_tool_call_ids"] == [second]


async def test_a_fully_rejected_round_resumes_as_rejected(client, db, admin_user, monkeypatch):
    calls = _capture(monkeypatch)
    chat_id, (first, second) = await _pending_round(db, admin_user, datetime.now(UTC) + timedelta(hours=1), asked=2)
    h = auth_headers(admin_user)
    await client.post(f"/chats/{chat_id}/approve-tool/{first}", json={"approved": False}, headers=h)
    r = await client.post(f"/chats/{chat_id}/approve-tool/{second}", json={"approved": False}, headers=h)
    assert r.json()["resume"] == "queued"
    assert len(calls) == 1 and calls[0]["approved"] is False
    assert sorted(calls[0]["rejected_tool_call_ids"]) == sorted([first, second])


async def test_an_expired_sibling_counts_as_rejected(client, db, admin_user, monkeypatch):
    """The other asked call's deadline passed but the sweep has not run yet:
    it can never be approved now, so the decision is complete."""
    calls = _capture(monkeypatch)
    chat_id, (first, second) = await _pending_round(db, admin_user, datetime.now(UTC) + timedelta(hours=1), asked=2)
    row = (await db.execute(select(PendingToolApproval).where(PendingToolApproval.tool_call_id == second))).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db.commit()
    r = await client.post(f"/chats/{chat_id}/approve-tool/{first}", json={"approved": True}, headers=auth_headers(admin_user))
    assert r.json()["resume"] == "queued"
    assert len(calls) == 1 and calls[0]["rejected_tool_call_ids"] == [second]
