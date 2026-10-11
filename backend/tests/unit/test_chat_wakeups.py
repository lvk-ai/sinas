"""Background work and wakeups (`background` system tool).

Contracts: delivery injects into a running or suspended chat and starts a
turn on an idle one; timers fire once when due; watches deliver POSTed
payloads until they expire; run_in_background queues a job whose result
either completes the suspended round (wait=true) or is delivered into the
chat; discovery binds the tools only when opted in.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal, async_engine
from app.models.chat import Chat, Message
from app.models.chat_wakeup import TIMER, WATCH, ChatWakeup
from app.models.pending_completion import PendingCompletion
from app.models.user import Role, RolePermission, User, UserRole
from app.services import chat_steering, chat_wakeups
from app.services import deferred_completions as dc
from app.services.delegation import current_channel_id
from app.services.message_service import MessageService
from app.services.queue_service import queue_service
from tests.conftest import auth_headers


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine_per_test():
    yield
    await async_engine.dispose()


@pytest.fixture(autouse=True)
def _tmp_file_storage(tmp_path, monkeypatch):
    """Workbench writes go to a temp dir, never the deployment's storage path."""
    import app.services.file_storage as fs

    monkeypatch.setenv("FILE_STORAGE_PATH", str(tmp_path / "files"))
    fs._storage = None
    yield
    fs._storage = None


@pytest_asyncio.fixture
async def chat(db: AsyncSession, test_user: User) -> Chat:
    c = Chat(user_id=test_user.id, title="wakeups chat")
    db.add(c)
    await db.flush()
    await db.refresh(c)
    return c


@pytest_asyncio.fixture
async def committed_chat():
    """Committed user + chat for code paths that open their own sessions."""
    async with AsyncSessionLocal() as s:
        user = User(email=f"wake-{uuid.uuid4().hex[:8]}@example.com")
        s.add(user)
        await s.flush()
        role = Role(name=f"wake-role-{uuid.uuid4().hex[:8]}", description="t")
        s.add(role)
        await s.flush()
        s.add(RolePermission(role_id=role.id, permission_key="sinas.*:all", permission_value=True))
        s.add(UserRole(role_id=role.id, user_id=user.id, active=True))
        chat = Chat(user_id=user.id, title="wake chat")
        s.add(chat)
        await s.commit()
        await s.refresh(user)
        await s.refresh(chat)
        env = {"user": user, "chat": chat}
    yield env
    async with AsyncSessionLocal() as s:
        cid, uid, rid = env["chat"].id, env["user"].id, role.id
        await s.execute(delete(ChatWakeup).where(ChatWakeup.chat_id == cid))
        from app.models.pending_approval import PendingToolApproval

        await s.execute(delete(PendingCompletion).where(PendingCompletion.chat_id == cid))
        await s.execute(delete(PendingToolApproval).where(PendingToolApproval.chat_id == cid))
        await s.execute(delete(Message).where(Message.chat_id == cid))
        await s.execute(delete(Chat).where(Chat.id == cid))
        await s.execute(delete(UserRole).where(UserRole.user_id == uid))
        await s.execute(delete(RolePermission).where(RolePermission.role_id == rid))
        await s.execute(delete(Role).where(Role.id == rid))
        await s.execute(delete(User).where(User.id == uid))
        await s.commit()


def _capture_enqueue(monkeypatch):
    calls = []

    async def fake(**kwargs):
        calls.append(kwargs)
        return "job-x"

    monkeypatch.setattr(queue_service, "enqueue_agent_message", fake)
    return calls


async def _messages(chat_id):
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(Message).where(Message.chat_id == chat_id).order_by(Message.created_at))).scalars().all()
        return [(m.role, m.content) for m in rows]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discovery_binds_background_tools_only_when_opted_in(db, test_user, monkeypatch):
    from app.models.agent import Agent
    from app.services.tool_discovery import get_available_tools

    monkeypatch.setattr(settings, "code_execution_enabled", True)

    async def names(system_tools):
        agent = Agent(namespace="test", name=f"bg-{uuid.uuid4().hex[:6]}", user_id=test_user.id, system_prompt="x", system_tools=system_tools)
        db.add(agent)
        await db.flush()
        c = Chat(user_id=test_user.id, agent_id=agent.id, title="t")
        db.add(c)
        await db.flush()
        svc = MessageService(db)
        tools = await get_available_tools(
            db=db, user_id=str(test_user.id), chat=c,
            function_converter=svc.function_converter, query_converter=svc.query_converter,
            skill_converter=svc.skill_converter, component_converter=svc.component_converter,
            collection_converter=svc.collection_converter, connector_converter=svc.connector_converter,
        )
        return {t["function"]["name"] for t in tools}

    assert not (chat_wakeups.BACKGROUND_TOOL_NAMES & await names([]))
    only_bg = await names(["background"])
    assert {"schedule_wakeup", "create_watch", "list_wakeups"} <= only_bg
    assert "run_in_background" not in only_bg  # needs codeExecution too
    assert "run_in_background" in await names(["background", "codeExecution"])


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delivery_injects_into_a_running_chat(committed_chat, monkeypatch):
    env = committed_chat
    cid = str(env["chat"].id)
    enqueued = _capture_enqueue(monkeypatch)
    token = await chat_steering.acquire_chat_lock(cid)
    try:
        out = await chat_wakeups.deliver_to_chat(cid, "[wakeup x] ping", source="wakeup")
    finally:
        await chat_steering.release_chat_lock(cid, token)
    assert out["status"] == "injected"
    assert await _messages(env["chat"].id) == [("user", "[wakeup x] ping")]
    assert enqueued == []


@pytest.mark.asyncio
async def test_delivery_injects_into_a_suspended_chat(committed_chat, monkeypatch):
    env = committed_chat
    cid = str(env["chat"].id)
    enqueued = _capture_enqueue(monkeypatch)
    async with AsyncSessionLocal() as s:
        await dc.suspend_round(
            s, chat_id=cid, user_id=str(env["user"].id), channel_id="c",
            entries={"call_q": {"completer": dc.HUMAN_INPUT, "question": "?"}},
            conversation_context={},
        )
    out = await chat_wakeups.deliver_to_chat(cid, "[watch w] payload", source="watch")
    assert out["status"] == "injected"
    assert enqueued == []  # a new turn would corrupt the parked round


@pytest.mark.asyncio
async def test_delivery_injects_while_a_completed_checkpoint_waits_to_be_queued(committed_chat, monkeypatch):
    """complete() records remaining == 0 before the saved turn is queued;
    a wakeup in that gap must not start a second turn beside it."""
    env = committed_chat
    cid = str(env["chat"].id)
    enqueued = _capture_enqueue(monkeypatch)
    async with AsyncSessionLocal() as s:
        row = await dc.suspend_round(
            s, chat_id=cid, user_id=str(env["user"].id), channel_id="c",
            entries={"call_q": {"completer": dc.HUMAN_INPUT, "question": "?"}},
            conversation_context={},
        )
        row.remaining = 0
        await s.commit()
    out = await chat_wakeups.deliver_to_chat(cid, "[watch w] payload", source="watch")
    assert out["status"] == "injected" and enqueued == []


@pytest.mark.asyncio
async def test_delivery_starts_a_turn_on_an_idle_chat(committed_chat, monkeypatch):
    env = committed_chat
    cid = str(env["chat"].id)
    enqueued = _capture_enqueue(monkeypatch)
    out = await chat_wakeups.deliver_to_chat(cid, "[wakeup y] go", source="wakeup", api_key_id=None)
    assert out["status"] == "started"
    (call,) = enqueued
    assert call["chat_id"] == cid and call["content"] == "[wakeup y] go"
    assert call["trigger_type"] == "wakeup" and call["user_token"]
    assert call["channel_id"] == out["channel_id"]
    async with AsyncSessionLocal() as s:
        c = await s.get(Chat, env["chat"].id)
        assert c.active_channel_id == out["channel_id"]


@pytest.mark.asyncio
async def test_turn_owner_claims_the_reconnect_channel(committed_chat, monkeypatch):
    """Two deliveries to an idle chat each recorded their own channel; the
    job that takes the lock (owns the turn) is the one a reconnecting client
    must find, whichever wrote `active_channel_id` last."""
    from app.services.delegation import current_channel_id
    from app.services.message_service import MessageService

    env = committed_chat
    cid = str(env["chat"].id)
    _capture_enqueue(monkeypatch)
    first = await chat_wakeups.deliver_to_chat(cid, "[wakeup a] one", source="wakeup")
    second = await chat_wakeups.deliver_to_chat(cid, "[wakeup b] two", source="wakeup")
    assert first["status"] == second["status"] == "started"
    async with AsyncSessionLocal() as s:
        assert (await s.get(Chat, env["chat"].id)).active_channel_id == second["channel_id"]

    async def no_turn(self, **kwargs):
        yield {"type": "done"}

    monkeypatch.setattr(MessageService, "_send_message_stream_locked", no_turn)
    token = current_channel_id.set(first["channel_id"])  # the first job runs the turn
    try:
        async with AsyncSessionLocal() as s:
            async for _ in MessageService(s).send_message_stream(
                chat_id=cid, user_id=str(env["user"].id), user_token="t", content="[wakeup a] one"
            ):
                pass
    finally:
        current_channel_id.reset(token)
    async with AsyncSessionLocal() as s:
        assert (await s.get(Chat, env["chat"].id)).active_channel_id == first["channel_id"]


@pytest.mark.asyncio
async def test_delivery_to_an_archived_chat_is_dropped(committed_chat, monkeypatch):
    env = committed_chat
    enqueued = _capture_enqueue(monkeypatch)
    async with AsyncSessionLocal() as s:
        c = await s.get(Chat, env["chat"].id)
        c.archived = True
        await s.commit()
    out = await chat_wakeups.deliver_to_chat(str(env["chat"].id), "x", source="wakeup")
    assert out["status"] == "dropped" and enqueued == []


# ---------------------------------------------------------------------------
# Timers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timer_validation(db, chat, test_user):
    assert "error" in await chat_wakeups.schedule_timer(db, chat, str(test_user.id), 0, "n")
    assert "error" in await chat_wakeups.schedule_timer(db, chat, str(test_user.id), settings.wakeup_max_delay_seconds + 1, "n")
    out = await chat_wakeups.schedule_timer(db, chat, str(test_user.id), 60, "check the deploy")
    assert out["wakeup_id"] and out["due_at"]
    listed = await chat_wakeups.list_wakeups(db, chat)
    assert [t["note"] for t in listed["timers"]] == ["check the deploy"]
    assert (await chat_wakeups.cancel_timer(db, chat, out["wakeup_id"]))["cancelled"]
    assert (await chat_wakeups.list_wakeups(db, chat))["timers"] == []
    assert "error" in await chat_wakeups.cancel_timer(db, chat, out["wakeup_id"])  # already gone
    assert "error" in await chat_wakeups.cancel_timer(db, chat, "not-a-uuid")


@pytest.mark.asyncio
async def test_due_timers_fire_once(committed_chat, monkeypatch):
    env = committed_chat
    delivered = []

    async def fake_deliver(chat_id, content, *, source, api_key_id=None):
        delivered.append((chat_id, content, source, api_key_id))
        return {"status": "started"}

    monkeypatch.setattr(chat_wakeups, "deliver_to_chat", fake_deliver)
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as s:
        s.add(ChatWakeup(chat_id=env["chat"].id, user_id=env["user"].id, kind=TIMER, note="due", due_at=now - timedelta(seconds=5), api_key_id="key-1"))
        s.add(ChatWakeup(chat_id=env["chat"].id, user_id=env["user"].id, kind=TIMER, note="later", due_at=now + timedelta(hours=1)))
        s.add(ChatWakeup(chat_id=env["chat"].id, user_id=env["user"].id, kind=TIMER, note="cancelled", due_at=now - timedelta(seconds=5), cancelled=True))
        await s.commit()

    assert await chat_wakeups.deliver_due_timers() == 1
    assert await chat_wakeups.deliver_due_timers() == 0  # not twice
    ((cid, content, source, key),) = delivered
    assert cid == str(env["chat"].id) and content.endswith("] due") and content.startswith("[wakeup ")
    assert source == "wakeup" and key == "key-1"


# ---------------------------------------------------------------------------
# Watches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watch_lifecycle_over_http(client, committed_chat, monkeypatch):
    env = committed_chat
    delivered = []

    async def fake_deliver(chat_id, content, *, source, api_key_id=None):
        delivered.append((chat_id, content, source))
        return {"status": "injected"}

    monkeypatch.setattr(chat_wakeups, "deliver_to_chat", fake_deliver)
    async with AsyncSessionLocal() as s:
        c = await s.get(Chat, env["chat"].id)
        out = await chat_wakeups.create_watch(s, c, str(env["user"].id), "CI finished", None)
    assert out["url"].startswith(settings.public_base_url + "/watch/")
    token = out["url"].rsplit("/", 1)[1]

    r = await client.post(f"/watch/{token}", json={"status": "green", "run": 42})
    assert r.status_code == 202, r.text
    assert r.json()["delivery"] == "injected"
    (cid, content, source) = delivered[-1]
    assert cid == str(env["chat"].id) and source == "watch"
    assert content.startswith(f"[watch {out['watch_id']}: CI finished]\n")
    assert json.loads(content.split("\n", 1)[1]) == {"status": "green", "run": 42}

    # plain text bodies are delivered as text
    r = await client.post(f"/watch/{token}", content=b"done", headers={"content-type": "text/plain"})
    assert r.status_code == 202 and delivered[-1][1].endswith("\ndone")

    # too large
    r = await client.post(f"/watch/{token}", content=b"x" * (settings.watch_max_payload_bytes + 1))
    assert r.status_code == 413

    # unknown token
    assert (await client.post("/watch/nope", json={})).status_code == 404

    # deleted -> gone
    async with AsyncSessionLocal() as s:
        c = await s.get(Chat, env["chat"].id)
        assert (await chat_wakeups.delete_watch(s, c, out["watch_id"]))["deleted"]
    assert (await client.post(f"/watch/{token}", json={})).status_code == 410

    # expired -> gone
    async with AsyncSessionLocal() as s:
        c = await s.get(Chat, env["chat"].id)
        out2 = await chat_wakeups.create_watch(s, c, str(env["user"].id), "old", 60)
        row = await s.get(ChatWakeup, uuid.UUID(out2["watch_id"]))
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await s.commit()
    assert (await client.post(f"/watch/{out2['url'].rsplit('/', 1)[1]}", json={})).status_code == 410


@pytest.mark.asyncio
async def test_watch_ttl_is_capped_and_validated(db, chat, test_user):
    assert "error" in await chat_wakeups.create_watch(db, chat, str(test_user.id), "d", 10)
    out = await chat_wakeups.create_watch(db, chat, str(test_user.id), "d", settings.watch_max_ttl_seconds * 10)
    exp = datetime.fromisoformat(out["expires_at"])
    assert exp <= datetime.now(timezone.utc) + timedelta(seconds=settings.watch_max_ttl_seconds + 5)


# ---------------------------------------------------------------------------
# Background runs
# ---------------------------------------------------------------------------

def _bg_call(call_id, wait=None, code="print(1)"):
    args = {"code": code, "description": "count things"}
    if wait is not None:
        args["wait"] = wait
    return {"id": call_id, "type": "function", "function": {"name": "run_in_background", "arguments": json.dumps(args)}}


async def _run_round(db, chat, user, tool_calls, monkeypatch, channel="chan-bg"):
    monkeypatch.setattr(settings, "code_execution_enabled", True)
    started = []

    async def fake_start(chat_id, user_id, code, description, *, tool_call_id=None, pending_completion_id=None, job_id=None):
        started.append({"chat_id": chat_id, "code": code, "tool_call_id": tool_call_id, "pending_completion_id": pending_completion_id, "job_id": job_id})
        return {"job_id": job_id or "job-1", "status": "queued", "description": description}

    monkeypatch.setattr(chat_wakeups, "start_background_job", fake_start)

    # A round that does not suspend continues with a follow-up model turn.
    class _Provider:
        async def stream(self, messages, model, tools, temperature, max_tokens):
            yield {"content": "ok", "tool_calls": None, "finish_reason": "stop"}

    async def fake_provider(*_a, **_k):
        return _Provider()

    from app.services import message_service as ms
    monkeypatch.setattr(ms, "create_provider", fake_provider)

    # The inline executor opens its own session; hand it the fixture's so it
    # sees the uncommitted chat.
    import contextlib

    from app.services import tool_execution

    @contextlib.asynccontextmanager
    async def _same_session():
        yield db

    monkeypatch.setattr(tool_execution, "AsyncSessionLocal", _same_session)
    svc = MessageService(db)
    token = current_channel_id.set(channel)
    chunks = []
    try:
        async for chunk in svc._handle_tool_calls(
            chat_id=str(chat.id), user_id=str(user.id), user_token="tok", messages=[],
            tool_calls=tool_calls, provider="openai", model="gpt-test", temperature=0.7,
            max_tokens=None, tools=chat_wakeups.get_background_tool_definitions(True), permissions={},
        ):
            chunks.append(chunk)
    finally:
        current_channel_id.reset(token)
    return chunks, started


@pytest.mark.asyncio
async def test_wait_true_suspends_the_round_on_a_background_job(db, chat, test_user, monkeypatch):
    chunks, started = await _run_round(db, chat, test_user, [_bg_call("call_bg", wait=True)], monkeypatch)
    by_type = {c.get("type"): c for c in chunks}
    assert dc.ROUND_SUSPENDED in by_type and by_type[dc.ROUND_SUSPENDED]["tool_call_ids"] == ["call_bg"]
    assert by_type["background_job_started"]["tool_call_id"] == "call_bg"
    row = (await db.execute(select(PendingCompletion).where(PendingCompletion.chat_id == chat.id))).scalar_one()
    assert dc.entry_kind(row.pending["call_bg"]) == dc.BACKGROUND_JOB
    assert row.pending["call_bg"]["expires_at"]  # deadline armed
    # The job was queued only after the checkpoint existed, against it, under
    # the id the checkpoint records (a timeout message points at a real job).
    (s,) = started
    assert s["pending_completion_id"] == str(row.id) and s["tool_call_id"] == "call_bg"
    assert s["job_id"] == row.pending["call_bg"]["job_id"]


@pytest.mark.asyncio
async def test_wait_false_runs_inline_and_returns_a_handle(db, chat, test_user, monkeypatch):
    chunks, started = await _run_round(db, chat, test_user, [_bg_call("call_bg2", wait=False)], monkeypatch)
    types = [c.get("type") for c in chunks]
    assert dc.ROUND_SUSPENDED not in types
    end = next(c for c in chunks if c.get("type") == "tool_end" and c["tool_call_id"] == "call_bg2")
    assert json.loads(end["result"])["job_id"] == "job-1"
    assert started[0]["pending_completion_id"] is None


@pytest.mark.asyncio
async def test_wait_true_with_empty_code_fails_fast(db, chat, test_user, monkeypatch):
    chunks, started = await _run_round(db, chat, test_user, [_bg_call("call_bad", wait=True, code="  ")], monkeypatch)
    ends = {c["tool_call_id"]: c for c in chunks if c.get("type") == "tool_end"}
    assert "error" in json.loads(ends["call_bad"]["result"])
    assert started == [] and dc.ROUND_SUSPENDED not in {c.get("type") for c in chunks}


@pytest.mark.asyncio
async def test_background_completer_timeout_names_the_job():
    content = json.loads(dc.get_completer(dc.BACKGROUND_JOB).timeout_content("c", {"job_id": "j9"}))
    assert content["timed_out"] and content["job_id"] == "j9"


@pytest.mark.asyncio
async def test_finished_job_is_delivered_into_the_chat(committed_chat, monkeypatch):
    from app.services import code_execution

    env = committed_chat
    delivered = []

    async def fake_exec(code, user_id=None, chat_id=None, timeout=None):
        return {"stdout": "42\n", "stderr": "", "result": 42, "duration_ms": 3}

    async def fake_deliver(chat_id, content, *, source, api_key_id=None):
        delivered.append((chat_id, content, source, api_key_id))
        return {"status": "started"}

    monkeypatch.setattr(code_execution, "execute", fake_exec)
    monkeypatch.setattr(chat_wakeups, "deliver_to_chat", fake_deliver)
    cid = str(env["chat"].id)
    await chat_wakeups.run_background_code_job(
        job_id="job-d", chat_id=cid, user_id=str(env["user"].id), code="42", description="answer",
        tool_call_id=None, pending_completion_id=None, api_key_id="k",
    )
    ((dcid, content, source, key),) = delivered
    assert dcid == cid and source == "background_job" and key == "k"
    assert content.startswith("[background job job-d finished: answer]\n")
    assert json.loads(content.split("\n", 1)[1])["result"] == 42
    status = await chat_wakeups.background_job_status(cid, "job-d")
    assert status["status"] == "finished" and status["result"]["result"] == 42
    # not readable from another chat
    assert "error" in await chat_wakeups.background_job_status(str(uuid.uuid4()), "job-d")


@pytest.mark.asyncio
async def test_finished_job_completes_the_suspended_round(committed_chat, monkeypatch):
    from app.services import code_execution

    env = committed_chat
    cid = str(env["chat"].id)
    completed = []

    async def fake_exec(code, user_id=None, chat_id=None, timeout=None):
        raise RuntimeError("sandbox exploded")

    async def fake_complete(pid, tool_call_id, content, *, user_token, resume_channel_id=None):
        completed.append((pid, tool_call_id, json.loads(content), bool(user_token)))
        return {"status": "completed", "resumed": True}

    monkeypatch.setattr(code_execution, "execute", fake_exec)
    monkeypatch.setattr(dc, "complete", fake_complete)
    await chat_wakeups.run_background_code_job(
        job_id="job-w", chat_id=cid, user_id=str(env["user"].id), code="boom", description="d",
        tool_call_id="call_w", pending_completion_id="pc-1", api_key_id=None,
    )
    ((pid, tcid, content, had_token),) = completed
    assert pid == "pc-1" and tcid == "call_w" and had_token
    assert content["error"] == "sandbox exploded"  # a crash still reports back
    assert (await chat_wakeups.background_job_status(cid, "job-w"))["status"] == "failed"


@pytest.mark.asyncio
async def test_a_large_waited_result_is_clipped_for_the_round(committed_chat, monkeypatch):
    """The full output stays behind background_job_status; what completes
    the parked round is bounded like any tool result."""
    from app.services import code_execution

    env = committed_chat
    cid = str(env["chat"].id)
    completed = []

    async def fake_exec(code, user_id=None, chat_id=None, timeout=None):
        return {"stdout": "x" * 50_000, "result": None}

    async def fake_complete(pid, tool_call_id, content, *, user_token, resume_channel_id=None):
        completed.append(json.loads(content))
        return {"status": "completed", "resumed": True}

    monkeypatch.setattr(code_execution, "execute", fake_exec)
    monkeypatch.setattr(dc, "complete", fake_complete)
    monkeypatch.setattr(settings, "background_job_result_max_chars", 2000)
    await chat_wakeups.run_background_code_job(
        job_id="job-big", chat_id=cid, user_id=str(env["user"].id), code="print('x'*50000)", description="d",
        tool_call_id="call_big", pending_completion_id="pc-2", api_key_id=None,
    )
    (content,) = completed
    assert content["truncated"] is True and content["job_id"] == "job-big"
    assert len(json.dumps(content)) < 3000 and "background_job_status" in content["hint"]
    full = await chat_wakeups.background_job_status(cid, "job-big")
    assert len(full["result"]["stdout"]) == 50_000


@pytest.mark.asyncio
async def test_start_background_job_enqueues_on_the_function_queue(committed_chat, monkeypatch):
    from app.core import redis as redis_mod

    env = committed_chat
    monkeypatch.setattr(settings, "code_execution_enabled", True)
    jobs = []

    class FakePool:
        async def enqueue_job(self, name, **kwargs):
            jobs.append((name, kwargs))

    async def fake_pool():
        return FakePool()

    monkeypatch.setattr(redis_mod, "get_arq_pool", fake_pool)
    cid = str(env["chat"].id)
    assert "error" in await chat_wakeups.start_background_job(cid, str(env["user"].id), "", "d")
    out = await chat_wakeups.start_background_job(cid, str(env["user"].id), "print(1)", "d")
    assert out["status"] == "queued" and "hint" in out
    ((name, kw),) = jobs
    assert name == "execute_background_code_job" and kw["_queue_name"] == chat_wakeups.BACKGROUND_QUEUE
    assert kw["job_id"] == out["job_id"] and kw["pending_completion_id"] is None
    listed = await chat_wakeups.list_background_jobs(cid)
    assert [j["job_id"] for j in listed] == [out["job_id"]]


@pytest.mark.asyncio
async def test_wait_true_resolves_workbench_references_in_code(db, test_user, monkeypatch):
    """A saved script runs the same whether the model waits on it or not."""
    from app.models.agent import Agent
    from app.services.workbench import WorkbenchTools

    agent = Agent(namespace="test", name=f"wb-{uuid.uuid4().hex[:6]}", user_id=test_user.id, system_tools=["workbench", "codeExecution", "background"])
    db.add(agent)
    await db.flush()
    chat = Chat(user_id=test_user.id, agent_id=agent.id, title="refs")
    db.add(chat)
    await db.flush()
    await db.refresh(chat)
    await WorkbenchTools().execute_tool(db, chat, str(test_user.id), "workbench_write", {"filename": "script.py", "content": "print('from file')"})
    call = {"id": "call_ref", "type": "function", "function": {"name": "run_in_background",
            "arguments": json.dumps({"code": {"$workbench": "script.py"}, "description": "d", "wait": True})}}
    chunks, started = await _run_round(db, chat, test_user, [call], monkeypatch)
    assert dc.ROUND_SUSPENDED in {c.get("type") for c in chunks}
    assert started[0]["code"] == "print('from file')"

    missing = {"id": "call_gone", "type": "function", "function": {"name": "run_in_background",
               "arguments": json.dumps({"code": {"$workbench": "nope.py"}, "description": "d", "wait": True})}}
    chunks, started = await _run_round(db, chat, test_user, [missing], monkeypatch)
    ends = {c["tool_call_id"]: c for c in chunks if c.get("type") == "tool_end"}
    assert "error" in json.loads(ends["call_gone"]["result"]) and started == []


@pytest.mark.asyncio
async def test_delivery_injects_while_an_approval_is_open(committed_chat, monkeypatch):
    from app.models.pending_approval import PendingToolApproval

    env = committed_chat
    cid = str(env["chat"].id)
    enqueued = _capture_enqueue(monkeypatch)
    async with AsyncSessionLocal() as s:
        anchor = Message(chat_id=env["chat"].id, role="assistant", content=None,
                         tool_calls=[{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}])
        s.add(anchor)
        await s.flush()
        s.add(PendingToolApproval(
            chat_id=env["chat"].id, message_id=anchor.id, user_id=env["user"].id, tool_call_id="c1",
            function_namespace="ns", function_name="t", arguments={}, all_tool_calls=[], conversation_context={},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ))
        await s.commit()
    out = await chat_wakeups.deliver_to_chat(cid, "[wakeup z] while approval open", source="wakeup")
    assert out["status"] == "injected" and enqueued == []


@pytest.mark.asyncio
async def test_cancelled_job_still_reports_back(committed_chat, monkeypatch):
    import asyncio

    from app.services import code_execution

    env = committed_chat
    cid = str(env["chat"].id)
    delivered = []

    async def fake_exec(code, user_id=None, chat_id=None, timeout=None):
        raise asyncio.CancelledError()

    async def fake_deliver(chat_id, content, *, source, api_key_id=None):
        delivered.append(content)
        return {"status": "started"}

    monkeypatch.setattr(code_execution, "execute", fake_exec)
    monkeypatch.setattr(chat_wakeups, "deliver_to_chat", fake_deliver)
    with pytest.raises(asyncio.CancelledError):
        await chat_wakeups.run_background_code_job(
            job_id="job-c", chat_id=cid, user_id=str(env["user"].id), code="x", description="d",
            tool_call_id=None, pending_completion_id=None, api_key_id=None,
        )
    assert delivered and delivered[0].startswith("[background job job-c failed: d]")
    assert (await chat_wakeups.background_job_status(cid, "job-c"))["status"] == "failed"



@pytest.mark.asyncio
async def test_inline_wait_is_refused_with_guidance(db, chat, test_user):
    out = await chat_wakeups.execute_background_tool(db, chat, str(test_user.id), "run_in_background", {"code": "1", "description": "d", "wait": True})
    assert "keep_alive" in out["error"]
