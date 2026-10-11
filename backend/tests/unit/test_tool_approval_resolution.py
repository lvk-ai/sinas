"""Approving a tool call resolves it only while it is still undecided and
before its deadline — the expiry sweep resolves it the same way, so a late
"approve" can never run a call the sweep already rejected."""

import pytest
import uuid
from datetime import UTC, datetime, timedelta

from app.models.chat import Chat, Message
from app.models.pending_approval import PendingToolApproval
from app.services.queue_service import queue_service
from tests.conftest import auth_headers


async def _pending(db, user, expires_at):
    chat = Chat(user_id=user.id, title="t", agent_namespace="ns", agent_name="bot")
    db.add(chat)
    await db.flush()
    anchor = Message(chat_id=chat.id, role="assistant", content="x")
    db.add(anchor)
    await db.flush()
    call_id = f"call-{uuid.uuid4().hex[:8]}"
    db.add(PendingToolApproval(
        chat_id=chat.id, message_id=anchor.id, user_id=user.id, tool_call_id=call_id,
        function_namespace="ops", function_name="purge", arguments={}, all_tool_calls=[],
        conversation_context={}, expires_at=expires_at,
    ))
    await db.flush()
    return str(chat.id), call_id


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


@pytest.mark.asyncio
async def test_same_tool_call_id_in_two_chats_is_fine(db, test_user):
    """Provider tool-call ids are only unique per response; two chats seeing
    the same id must both get their approval row (uniqueness is per chat)."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy.exc import IntegrityError

    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    rows = []
    for title in ("chat a", "chat b"):
        chat = Chat(user_id=test_user.id, title=title)
        db.add(chat)
        await db.flush()
        anchor = Message(chat_id=chat.id, role="assistant", content="x")
        db.add(anchor)
        await db.flush()
        row = PendingToolApproval(
            chat_id=chat.id, message_id=anchor.id, user_id=test_user.id,
            tool_call_id="call_promote", function_namespace="tool",
            function_name="workbench_promote", arguments={}, all_tool_calls=[],
            conversation_context={}, expires_at=expires_at,
        )
        db.add(row)
        await db.flush()
        rows.append(row)
    assert rows[0].chat_id != rows[1].chat_id

    # ...while the same id twice within one chat is still rejected.
    dup = PendingToolApproval(
        chat_id=rows[0].chat_id, message_id=rows[0].message_id, user_id=test_user.id,
        tool_call_id="call_promote", function_namespace="tool",
        function_name="workbench_promote", arguments={}, all_tool_calls=[],
        conversation_context={}, expires_at=expires_at,
    )
    db.add(dup)
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()
