"""GET /messages/stats: the dashboard numbers, from one grouped pass.

The endpoint used to run a query per figure, each scanning all of messages.
It now folds one grouped query into the total, the per-day series, the top
agents and the role split, and counts tool calls apart. The response is
unchanged; these tests pin it down, read:own scoping included.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.models import Chat, Message, User
from app.models.user import Role, RolePermission, UserRole
from tests.conftest import auth_headers


async def _stats(client, user, days=7):
    r = await client.get(f"/api/v1/messages/stats?days={days}", headers=auth_headers(user))
    assert r.status_code == 200, r.text
    return r.json()


def _delta(after: dict, before: dict) -> dict:
    """Per-key growth, dropping keys that did not change (other tests' rows)."""
    out = {k: after.get(k, 0) - before.get(k, 0) for k in after}
    return {k: v for k, v in out.items() if v}


async def _own_reader(db) -> User:
    role = Role(name=f"own-reader-{uuid.uuid4().hex[:8]}", description="read own executions")
    db.add(role)
    await db.flush()
    db.add(RolePermission(role_id=role.id, permission_key="sinas.executions.read:own", permission_value=True))
    user = User(email=f"own-{uuid.uuid4().hex[:8]}@example.com")
    db.add(user)
    await db.flush()
    db.add(UserRole(role_id=role.id, user_id=user.id, active=True))
    await db.flush()
    return user


async def _seed(db, owner: User, ns: str) -> None:
    """Two agents' chats with messages today, yesterday and out of the window."""
    now = datetime.now(timezone.utc)
    today, yesterday, old = now - timedelta(minutes=5), now - timedelta(days=1), now - timedelta(days=30)
    a = Chat(user_id=owner.id, agent_namespace=ns, agent_name="a", title="a")
    b = Chat(user_id=owner.id, agent_namespace=ns, agent_name="b", title="b")
    db.add_all([a, b])
    await db.flush()
    db.add_all([
        Message(chat_id=a.id, role="user", content="q", created_at=today),
        Message(chat_id=a.id, role="assistant", content=None, created_at=today,
                tool_calls=[{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]),
        Message(chat_id=a.id, role="tool", content="r", tool_call_id="c1", created_at=today),
        Message(chat_id=a.id, role="assistant", content="done", created_at=today),
        Message(chat_id=b.id, role="user", content="q", created_at=yesterday),
        Message(chat_id=b.id, role="assistant", content="a", created_at=yesterday),
        # Outside a 7-day window
        Message(chat_id=b.id, role="user", content="old", created_at=old),
    ])
    await db.flush()


async def test_stats_fold_one_pass_into_the_same_response(client, db):
    # A fresh read:own user sees only the chats seeded here, so every figure
    # is exact whatever else the database holds.
    reader = await _own_reader(db)
    ns = f"st{uuid.uuid4().hex[:6]}"
    await _seed(db, reader, ns)
    stats = await _stats(client, reader)

    assert stats["total_messages"] == 6
    assert stats["tool_calls"] == 1
    assert stats["role_distribution"] == {"user": 2, "assistant": 3, "tool": 1}

    # Days by the database's own date cast, as before; in order, oldest first
    day_rows = (await db.execute(text(
        "SELECT CAST(m.created_at AS DATE) AS d, count(*) FROM messages m JOIN chats c ON m.chat_id = c.id "
        "WHERE c.agent_namespace = :ns AND m.created_at >= now() - interval '7 days' GROUP BY 1 ORDER BY 1"
    ), {"ns": ns})).all()
    assert list(stats["activity_by_day"].items()) == [(str(d), n) for d, n in day_rows]

    assert stats["agent_usage"] == [{"agent": f"{ns}/a", "count": 4}, {"agent": f"{ns}/b", "count": 2}]

    assert set(stats) == {
        "total_messages", "tool_calls", "activity_by_day", "agent_usage", "role_distribution",
        "llm_prompt_tokens", "llm_completion_tokens", "llm_total_tokens",
        "llm_cache_read_tokens", "llm_cache_write_tokens", "llm_calls", "days",
    }


async def test_read_all_counts_every_users_messages(client, db, admin_user):
    # read:all sees whatever the database already holds, so compare growth.
    before = await _stats(client, admin_user)
    await _seed(db, await _own_reader(db), f"all{uuid.uuid4().hex[:6]}")
    after = await _stats(client, admin_user)

    assert after["total_messages"] - before["total_messages"] == 6
    assert after["tool_calls"] - before["tool_calls"] == 1
    assert _delta(after["role_distribution"], before["role_distribution"]) == {"user": 2, "assistant": 3, "tool": 1}
    assert sum(_delta(after["activity_by_day"], before["activity_by_day"]).values()) == 6
    assert list(after["activity_by_day"]) == sorted(after["activity_by_day"])
    counts = [u["count"] for u in after["agent_usage"]]
    assert counts == sorted(counts, reverse=True) and len(counts) <= 10


async def test_agent_usage_keeps_the_top_ten(client, db):
    reader = await _own_reader(db)
    ns = f"top{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc)
    # Twelve agents with i+1 messages each: the two smallest drop out.
    for i in range(12):
        chat = Chat(user_id=reader.id, agent_namespace=ns, agent_name=f"g{i:02d}", title="t")
        db.add(chat)
        await db.flush()
        await db.execute(text(
            "INSERT INTO messages (id, chat_id, role, created_at) "
            "SELECT gen_random_uuid(), :chat, 'user', :ts FROM generate_series(1, :n)"
        ), {"chat": chat.id, "ts": now, "n": i + 1})
    stats = await _stats(client, reader)
    assert stats["agent_usage"] == [{"agent": f"{ns}/g{i:02d}", "count": i + 1} for i in range(11, 1, -1)]


async def test_read_own_counts_only_own_chats(client, db, admin_user):
    reader = await _own_reader(db)
    await _seed(db, admin_user, f"other{uuid.uuid4().hex[:6]}")
    ns = f"own{uuid.uuid4().hex[:6]}"
    await _seed(db, reader, ns)

    stats = await _stats(client, reader)
    assert stats["total_messages"] == 6
    assert stats["tool_calls"] == 1
    assert stats["role_distribution"] == {"user": 2, "assistant": 3, "tool": 1}
    assert sum(stats["activity_by_day"].values()) == 6
    assert stats["agent_usage"] == [{"agent": f"{ns}/a", "count": 4}, {"agent": f"{ns}/b", "count": 2}]

    # A wider window reaches the 30-day-old message
    assert (await _stats(client, reader, days=31))["total_messages"] == 7


async def test_stats_indexes_exist(db):
    """The migration and the model declare the same two indexes, and the
    migration keeps the newest pages all-visible for index-only scans."""
    rows = dict((await db.execute(text(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'messages' "
        "AND indexname IN ('ix_messages_created_at', 'ix_messages_created_at_tool_calls')"
    ))).all())
    assert "INCLUDE (chat_id, role)" in rows["ix_messages_created_at"]
    assert "WHERE (tool_calls IS NOT NULL)" in rows["ix_messages_created_at_tool_calls"]
    reloptions = (await db.execute(text("SELECT reloptions FROM pg_class WHERE relname = 'messages'"))).scalar()
    assert "autovacuum_vacuum_insert_scale_factor=0.01" in reloptions
