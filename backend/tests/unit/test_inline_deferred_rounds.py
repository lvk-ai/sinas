"""Deferred tool rounds in inline turns (ADR 2026-10-20).

A chat without keep-alive runs its turn inside the HTTP request — no queue
job, no job channel. Its rounds used to error on ask_user and
run_in_background(wait=true) ("unavailable in this synchronous context")
while the tools were still offered. Now every way of starting a turn parks
the round the same way the queued path does, and says so:

- inline stream: input_required / round_suspended events, then `done`
  {"status": "suspended"} — or, for a round waiting on a background run, the
  continuation relayed from the round's channel;
- non-streaming send and invoke: status "input_required" with the open
  questions, or — for a background run — the continuation followed to the
  final reply (status "in_progress" with the channel past the time budget);
- the answer endpoint resumes on a fresh channel, or with `wait: true`
  returns the resumed turn's result;
- the OpenAI adapter renders the question and takes the next message as the
  answer; a pipeline step fails clearly instead of timing out.

The LLM is scripted per chat; the resume job is the real
execute_agent_delegate_resume_job, run in-process where a worker would run
it (its own context, as in another process). Everything else is the real
stack against Postgres and Redis.
"""
import asyncio
import contextvars
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update

from app.core.auth import bind_api_key, current_api_key_id, reset_api_key
from app.core.config import settings
from app.core.database import AsyncSessionLocal, async_engine
from app.models import LLMUsage
from app.models.agent import Agent
from app.models.chat import Chat, Message
from app.models.pending_completion import PendingCompletion
from app.models.user import Role, RolePermission, User, UserRole
from app.providers.base import BaseLLMProvider
from app.providers.tracking import UsageTrackingProvider
from app.services import chat_steering, chat_wakeups, deferred_completions, sync_turns
from app.services import message_service as ms
from app.services.queue_service import queue_service
from app.services.stream_relay import stream_relay
from tests.conftest import auth_headers

USAGE = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}


# ---------------------------------------------------------------------------
# The scripted model
# ---------------------------------------------------------------------------


def _call(name: str, arguments: dict) -> dict:
    return {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def ask(question: str, options: Optional[list[str]] = None) -> dict:
    args: dict[str, Any] = {"question": question}
    if options:
        args["options"] = options
    return {"content": None, "tool_calls": [_call("ask_user", args)]}


def wait_on_background(code: str = "print(6 * 7)") -> dict:
    return {
        "content": None,
        "tool_calls": [_call("run_in_background", {"code": code, "description": "compute", "wait": True})],
    }


def say(text: str) -> dict:
    return {"content": text, "tool_calls": None}


class ScriptedLLM(BaseLLMProvider):
    """Pops the next step scripted for the chat it serves, for the first
    (complete) call of a non-streaming send and every streamed call."""

    scripts: dict[str, list[dict]] = {}
    seen: dict[str, list[list[dict]]] = {}

    def __init__(self, chat_id: str):
        super().__init__(api_key="k")
        self._chat_id = chat_id

    def _next(self, messages) -> dict:
        self.seen.setdefault(self._chat_id, []).append(messages)
        steps = self.scripts.get(self._chat_id)
        assert steps, f"the scripted model was asked for more rounds than planned ({self._chat_id})"
        return steps.pop(0)

    async def complete(self, messages, model, tools=None, temperature=0.7, max_tokens=None, **kw):
        step = self._next(messages)
        return {
            "content": step["content"],
            "tool_calls": step["tool_calls"],
            "usage": USAGE,
            "finish_reason": "tool_calls" if step["tool_calls"] else "stop",
        }

    async def stream(self, messages, model, tools=None, temperature=0.7, max_tokens=None, **kw):
        step = self._next(messages)
        deltas = None
        if step["tool_calls"]:
            deltas = [{"index": i, **tc} for i, tc in enumerate(step["tool_calls"])]
        yield {"content": step["content"], "tool_calls": deltas, "finish_reason": None}
        yield {"content": None, "tool_calls": None, "usage": USAGE, "finish_reason": "stop"}

    def format_tool_calls(self, tool_calls):
        return tool_calls or []

    def extract_usage(self, response):
        return {}


def script(chat_id: str, *steps: dict) -> None:
    ScriptedLLM.scripts[str(chat_id)] = list(steps)


@pytest_asyncio.fixture(autouse=True)
async def _scripted_llm(monkeypatch):
    async def fake_create_provider(provider_name, model, db, usage_context=None, **kw):
        return UsageTrackingProvider(
            ScriptedLLM(str((usage_context or {}).get("chat_id"))),
            provider_name=None,
            provider_type="openai",
            context=usage_context,
        )

    monkeypatch.setattr(ms, "create_provider", fake_create_provider)
    monkeypatch.setattr(settings, "compaction_enabled", False)
    ScriptedLLM.scripts = {}
    ScriptedLLM.seen = {}
    yield
    await async_engine.dispose()


# ---------------------------------------------------------------------------
# An in-process worker for the round-resume job
# ---------------------------------------------------------------------------


class Worker:
    def __init__(self):
        self.jobs: list[dict[str, Any]] = []
        self.tasks: list[asyncio.Task] = []

    async def drain(self, timeout: float = 15) -> None:
        while any(not t.done() for t in self.tasks):
            await asyncio.wait_for(asyncio.gather(*self.tasks), timeout)


@pytest_asyncio.fixture
async def worker(monkeypatch):
    """Queue the round-resume job onto an in-process "worker": the real job
    function, run in a fresh context (as a separate process would), with
    the kwargs the real enqueue gives it — the acting API key included."""
    from app.queue.agent_jobs import execute_agent_delegate_resume_job

    w = Worker()

    async def enqueue(*, chat_id, user_id, user_token, channel_id, conversation_context,
                      job_id=None, **_):
        job = {
            "job_id": job_id or str(uuid.uuid4()),
            "chat_id": chat_id,
            "user_id": user_id,
            "user_token": user_token,
            "channel_id": channel_id,
            "conversation_context": conversation_context,
            "api_key_id": current_api_key_id(),
        }
        w.jobs.append(job)
        w.tasks.append(
            asyncio.create_task(
                execute_agent_delegate_resume_job({}, **job), context=contextvars.Context()
            )
        )
        return job["job_id"]

    monkeypatch.setattr(queue_service, "enqueue_agent_delegate_resume", enqueue)
    yield w
    await w.drain()


@pytest.fixture
def background_runs(monkeypatch):
    """run_in_background without a sandbox: the "run" finishes shortly after
    it is queued and completes the round's checkpoint entry, as the
    background worker does. `hold` keeps runs from finishing."""
    state = {"hold": False, "tasks": [], "started": []}
    monkeypatch.setattr(settings, "code_execution_enabled", True)

    async def start_background_job(chat_id, user_id, code, description, *, tool_call_id=None,
                                   pending_completion_id=None, job_id=None):
        state["started"].append({"tool_call_id": tool_call_id, "pending_completion_id": pending_completion_id})

        async def finish():
            await asyncio.sleep(0.2)
            await deferred_completions.complete(
                pending_completion_id,
                tool_call_id,
                json.dumps({"job_id": job_id, "status": "completed", "stdout": "42\n"}),
                user_token="",
            )

        if not state["hold"]:
            state["tasks"].append(asyncio.create_task(finish(), context=contextvars.Context()))
        return {"job_id": job_id, "status": "queued", "description": description}

    monkeypatch.setattr(chat_wakeups, "start_background_job", start_background_job)
    return state


# ---------------------------------------------------------------------------
# Committed world: a user, agents, chats over the real API
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def env():
    created_agents: list[uuid.UUID] = []
    async with AsyncSessionLocal() as s:
        role = Role(name=f"inline-defer-{uuid.uuid4().hex[:8]}", description="test")
        s.add(role)
        await s.flush()
        s.add(RolePermission(role_id=role.id, permission_key="sinas.*:all", permission_value=True))
        user = User(email=f"inline-defer-{uuid.uuid4().hex[:8]}@example.com")
        s.add(user)
        await s.flush()
        s.add(UserRole(role_id=role.id, user_id=user.id, active=True))
        await s.commit()
        await s.refresh(user)

    async def make_agent(system_tools=("askUser",), **fields) -> Agent:
        async with AsyncSessionLocal() as s:
            agent = Agent(
                namespace="inline-defer",
                name=f"a-{uuid.uuid4().hex[:10]}",
                model="scripted",
                system_prompt="You are a test agent.",
                system_tools=list(system_tools),
                is_active=True,
                **fields,
            )
            s.add(agent)
            await s.commit()
            await s.refresh(agent)
        created_agents.append(agent.id)
        return agent

    yield {"user": user, "make_agent": make_agent}

    async with AsyncSessionLocal() as s:
        from app.models.webhook import Webhook

        await s.execute(delete(Webhook).where(Webhook.user_id == user.id))
        chat_ids = (await s.execute(select(Chat.id).where(Chat.user_id == user.id))).scalars().all()
        if chat_ids:
            await s.execute(delete(PendingCompletion).where(PendingCompletion.chat_id.in_(chat_ids)))
            await s.execute(delete(LLMUsage).where(LLMUsage.chat_id.in_(chat_ids)))
            await s.execute(delete(Message).where(Message.chat_id.in_(chat_ids)))
            await s.execute(delete(Chat).where(Chat.id.in_(chat_ids)))
        if created_agents:
            await s.execute(delete(Agent).where(Agent.id.in_(created_agents)))
        await s.execute(delete(UserRole).where(UserRole.user_id == user.id))
        await s.execute(delete(User).where(User.id == user.id))
        await s.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
        await s.execute(delete(Role).where(Role.id == role.id))
        await s.commit()


@pytest_asyncio.fixture
async def http():
    from app.main import app

    app.dependency_overrides.clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _new_chat(http, user, agent) -> str:
    resp = await http.post(
        f"/agents/{agent.namespace}/{agent.name}/chats", json={}, headers=auth_headers(user)
    )
    assert resp.status_code == 200, resp.text
    assert not resp.json()["keep_alive"]  # an inline chat
    return resp.json()["id"]


async def _session_chat(user, agent, session_key: str) -> str:
    """The chat an invoke with this session_key uses, made up front so its
    script can be keyed by id."""
    async with AsyncSessionLocal() as s:
        chat = Chat(
            user_id=user.id, agent_id=agent.id, agent_namespace=agent.namespace,
            agent_name=agent.name, title=f"invoke:{session_key}", session_key=session_key,
        )
        s.add(chat)
        await s.commit()
        return str(chat.id)


async def _sse(http, method: str, url: str, user, **kw) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    async with http.stream(method, url, headers=auth_headers(user), **kw) as resp:
        assert resp.status_code == 200
        event = None
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event != "ping":
                raw = line[5:].strip()
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = raw
                events.append((event, data))
                if event in ("done", "error"):
                    break
    return events


async def _channel_events(channel_id: str):
    """What a channel holds so far."""
    from app.core.redis import get_redis
    from app.services.stream_relay import STREAM_PREFIX

    for _, fields in await (await get_redis()).xrange(f"{STREAM_PREFIX}{channel_id}"):
        yield json.loads(fields["data"])


async def _transcript(chat_id: str) -> list[Message]:
    async with AsyncSessionLocal() as s:
        return list(
            (await s.execute(
                select(Message).where(Message.chat_id == uuid.UUID(chat_id)).order_by(Message.created_at)
            )).scalars()
        )


async def _checkpoint(chat_id: str) -> Optional[PendingCompletion]:
    async with AsyncSessionLocal() as s:
        return (
            await s.execute(select(PendingCompletion).where(PendingCompletion.chat_id == uuid.UUID(chat_id)))
        ).scalar_one_or_none()


async def _chat(chat_id: str) -> Chat:
    async with AsyncSessionLocal() as s:
        return await s.get(Chat, uuid.UUID(chat_id))


# ---------------------------------------------------------------------------
# Inline streaming
# ---------------------------------------------------------------------------


async def test_inline_stream_parks_on_ask_user_and_ends_cleanly(env, http, worker):
    user, agent = env["user"], await env["make_agent"]()
    chat_id = await _new_chat(http, user, agent)
    script(chat_id, ask("Which region?", ["eu", "us"]), say("Deploying to eu."))

    events = await asyncio.wait_for(
        _sse(http, "POST", f"/chats/{chat_id}/messages/stream", user, json={"content": "deploy it"}),
        timeout=20,
    )
    kinds = [e for e, _ in events]
    assert kinds[-3:] == ["input_required", "round_suspended", "done"]
    asked = dict(events)["input_required"]
    assert asked["question"] == "Which region?" and asked["options"] == ["eu", "us"]
    suspended = dict(events)["round_suspended"]
    assert events[-1][1] == {"status": "suspended"}

    # Parked exactly as a queued round parks: a checkpoint on the round's
    # own (fresh) channel, recorded as the chat's reconnect channel; the
    # chat lock given back; no tool result yet.
    row = await _checkpoint(chat_id)
    assert row is not None and row.channel_id == suspended["channel_id"]
    assert (await _chat(chat_id)).active_channel_id == row.channel_id
    assert not await chat_steering.is_chat_locked(chat_id)
    assert [m.role for m in await _transcript(chat_id)] == ["user", "assistant"]

    # The answer resumes the round on a fresh channel, like a queued round.
    resp = await http.post(
        f"/chats/{chat_id}/pending-input/{asked['tool_call_id']}",
        json={"answer": "eu"}, headers=auth_headers(user),
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["resumed"] is True and body["channel_id"] != row.channel_id
    await worker.drain()
    followed = await _sse(http, "GET", f"/chats/{chat_id}/stream/{body['channel_id']}", user)
    assert followed[-1] == ("done", {"status": "completed"})
    assert "Deploying to eu." in "".join(
        d.get("content") or "" for e, d in followed if e == "message" and isinstance(d, dict)
    )

    # The round's own channel points readers at the one it resumed on.
    moved = [e async for e in _channel_events(row.channel_id)]
    assert {"type": "round_moved", "channel_id": body["channel_id"]} in moved

    roles = [(m.role, m.name) for m in await _transcript(chat_id)]
    assert roles == [("user", None), ("assistant", None), ("tool", "ask_user"), ("assistant", None)]
    assert (await _transcript(chat_id))[-1].content == "Deploying to eu."
    assert await _checkpoint(chat_id) is None


async def test_inline_stream_relays_a_background_continuation(env, http, worker, background_runs):
    user = env["user"]
    agent = await env["make_agent"](system_tools=("background", "codeExecution"))
    chat_id = await _new_chat(http, user, agent)
    script(chat_id, wait_on_background(), say("The answer is 42."))

    events = await asyncio.wait_for(
        _sse(http, "POST", f"/chats/{chat_id}/messages/stream", user, json={"content": "compute"}),
        timeout=30,
    )
    kinds = [e for e, _ in events]
    # Not parked on the user: the stream stays open and relays the
    # continuation from the round's channel, ending as a turn ends.
    assert "round_suspended" in kinds
    assert events[-1] == ("done", {"status": "completed"})
    assert "The answer is 42." in "".join(
        d.get("content") or "" for e, d in events if e == "message" and isinstance(d, dict)
    )
    assert (await _transcript(chat_id))[-1].content == "The answer is 42."
    (job,) = worker.jobs
    assert job["channel_id"] == dict(events)["round_suspended"]["channel_id"]


async def test_an_open_stream_follows_its_round_to_the_answers_channel(
    env, http, worker, background_runs
):
    """A round that asks and also waits on a background run keeps its stream
    open (the run's result lands there). Once the run is in, the answer
    resumes the round on a fresh channel: the open stream follows it there
    and ends with the reply instead of idling on the old channel."""
    user = env["user"]
    agent = await env["make_agent"](system_tools=("askUser", "background", "codeExecution"))
    chat_id = await _new_chat(http, user, agent)
    both = {"content": None, "tool_calls": ask("Which region?")["tool_calls"] + wait_on_background()["tool_calls"]}
    script(chat_id, both, say("Deploying to eu with 42."))

    reading = asyncio.create_task(
        _sse(http, "POST", f"/chats/{chat_id}/messages/stream", user, json={"content": "go"})
    )
    for _ in range(100):  # until the run has reported back
        row = await _checkpoint(chat_id)
        if row is not None and row.remaining == 1:
            break
        await asyncio.sleep(0.1)
    (question_id,) = row.pending
    resp = await http.post(
        f"/chats/{chat_id}/pending-input/{question_id}", json={"answer": "eu"}, headers=auth_headers(user),
    )
    assert resp.status_code == 202 and resp.json()["resumed"]

    events = await asyncio.wait_for(reading, timeout=30)
    assert events[-1] == ("done", {"status": "completed"})
    assert "Deploying to eu with 42." in "".join(
        d.get("content") or "" for e, d in events if e == "message" and isinstance(d, dict)
    )
    assert not any(e == "message" and d.get("type") == "round_moved" for e, d in events)


# ---------------------------------------------------------------------------
# Non-streaming send, and the waited answer
# ---------------------------------------------------------------------------


async def test_send_reports_the_question_and_a_waited_answer_returns_the_reply(env, http, worker):
    user, agent = env["user"], await env["make_agent"]()
    chat_id = await _new_chat(http, user, agent)
    script(chat_id, ask("Which region?", ["eu", "us"]), say("Deploying to eu."))

    resp = await http.post(f"/chats/{chat_id}/messages", json={"content": "deploy it"}, headers=auth_headers(user))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "input_required"
    (question,) = out["pending_inputs"]
    assert question["question"] == "Which region?" and question["options"] == ["eu", "us"]
    assert out["role"] == "assistant" and out["tool_calls"][0]["id"] == question["tool_call_id"]
    assert out["channel_id"] is None
    assert out["usage"]["calls"] == 1  # the call that asked, nothing more

    resp = await http.post(
        f"/chats/{chat_id}/pending-input/{question['tool_call_id']}",
        json={"answer": "eu", "wait": True}, headers=auth_headers(user),
    )
    assert resp.status_code == 200, resp.text
    done = resp.json()
    assert done["status"] == "completed"
    assert done["reply"] == "Deploying to eu." and done["message"]["content"] == "Deploying to eu."
    assert done["pending_inputs"] == [] and done["channel_id"] is None
    # The continuation's call, reported by the resume job that made it.
    assert done["usage"]["calls"] == 1 and done["usage"]["total_tokens"] == 12

    # The answer is the tool result the follow-up turn was rebuilt from.
    (followup,) = ScriptedLLM.seen[chat_id][1:]
    assert followup[-1]["role"] == "tool" and json.loads(followup[-1]["content"]) == {"answer": "eu"}


async def test_a_waited_answer_that_is_asked_again_returns_the_next_question(env, http, worker):
    user, agent = env["user"], await env["make_agent"]()
    chat_id = await _new_chat(http, user, agent)
    script(chat_id, ask("Which region?"), ask("Which tier?"), say("Done."))

    out = (await http.post(f"/chats/{chat_id}/messages", json={"content": "go"}, headers=auth_headers(user))).json()
    first = out["pending_inputs"][0]["tool_call_id"]
    again = (await http.post(
        f"/chats/{chat_id}/pending-input/{first}", json={"answer": "eu", "wait": True}, headers=auth_headers(user),
    )).json()
    assert again["status"] == "input_required"
    assert [q["question"] for q in again["pending_inputs"]] == ["Which tier?"]
    final = (await http.post(
        f"/chats/{chat_id}/pending-input/{again['pending_inputs'][0]['tool_call_id']}",
        json={"answer": "gold", "wait": True}, headers=auth_headers(user),
    )).json()
    assert (final["status"], final["reply"]) == ("completed", "Done.")


# ---------------------------------------------------------------------------
# Invoke
# ---------------------------------------------------------------------------


async def test_invoke_reports_the_question_then_takes_the_answer(env, http, worker):
    user, agent = env["user"], await env["make_agent"]()
    chat_id = await _session_chat(user, agent, "slack:T1")
    script(chat_id, ask("Which order number?"), say("Order 123 shipped."))

    resp = await http.post(
        f"/agents/{agent.namespace}/{agent.name}/invoke",
        json={"message": "where is my order", "session_key": "slack:T1"}, headers=auth_headers(user),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["chat_id"] == chat_id and out["status"] == "input_required"
    assert out["reply"] == ""
    (question,) = out["pending_inputs"]
    assert question["question"] == "Which order number?"
    assert out["usage"]["calls"] == 1

    # Invoking the session again while it waits does not run (or echo) a
    # turn: the message is delivered into the parked one, and the response
    # says what it still waits on.
    again = (await http.post(
        f"/agents/{agent.namespace}/{agent.name}/invoke",
        json={"message": "hello?", "session_key": "slack:T1"}, headers=auth_headers(user),
    )).json()
    assert again["status"] == "injected" and again["reply"] == ""
    assert [q["tool_call_id"] for q in again["pending_inputs"]] == [question["tool_call_id"]]
    assert again["channel_id"] == (await _checkpoint(chat_id)).channel_id
    assert again["usage"]["calls"] == 0

    done = (await http.post(
        f"/chats/{chat_id}/pending-input/{question['tool_call_id']}",
        json={"answer": "123", "wait": True}, headers=auth_headers(user),
    )).json()
    assert (done["status"], done["reply"]) == ("completed", "Order 123 shipped.")


async def test_invoke_follows_a_background_wait_to_the_final_reply(env, http, worker, background_runs):
    user = env["user"]
    agent = await env["make_agent"](system_tools=("background", "codeExecution"))
    chat_id = await _session_chat(user, agent, "bg")
    script(chat_id, wait_on_background(), say("The answer is 42."))

    resp = await http.post(
        f"/agents/{agent.namespace}/{agent.name}/invoke",
        json={"message": "compute", "session_key": "bg"}, headers=auth_headers(user),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert (out["status"], out["reply"]) == ("completed", "The answer is 42.")
    assert out["pending_inputs"] == [] and out["channel_id"] is None
    # Both calls: the one the request made, and the continuation's, which
    # the resume job reported on the channel this request followed.
    assert out["usage"]["calls"] == 2 and out["usage"]["total_tokens"] == 24
    (started,) = background_runs["started"]
    tool_rows = [m for m in await _transcript(chat_id) if m.role == "tool"]
    assert [m.tool_call_id for m in tool_rows] == [started["tool_call_id"]]


async def test_invoke_past_its_budget_reports_the_turn_in_progress(env, http, worker, background_runs):
    user = env["user"]
    agent = await env["make_agent"](system_tools=("background", "codeExecution"), default_job_timeout=1)
    chat_id = await _session_chat(user, agent, "slow")
    background_runs["hold"] = True  # the run never finishes within the budget
    script(chat_id, wait_on_background())

    out = (await http.post(
        f"/agents/{agent.namespace}/{agent.name}/invoke",
        json={"message": "compute", "session_key": "slow"}, headers=auth_headers(user),
    )).json()
    assert out["status"] == "in_progress"
    row = await _checkpoint(chat_id)
    assert out["channel_id"] == row.channel_id == (await _chat(chat_id)).active_channel_id
    # The turn goes on past this request: its total is not known here.
    assert out["usage"] is None


# ---------------------------------------------------------------------------
# Expiry, API key, delegation, queued path
# ---------------------------------------------------------------------------


async def test_expiry_resumes_an_inline_started_round_on_its_channel(env, http, worker):
    user, agent = env["user"], await env["make_agent"]()
    chat_id = await _new_chat(http, user, agent)
    script(chat_id, ask("Which region?"), say("No answer; assuming eu."))
    out = (await http.post(f"/chats/{chat_id}/messages", json={"content": "go"}, headers=auth_headers(user))).json()
    assert out["status"] == "input_required"
    row = await _checkpoint(chat_id)

    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    pending = {k: {**v, "expires_at": past} for k, v in row.pending.items()}
    async with AsyncSessionLocal() as s:
        await s.execute(
            update(PendingCompletion).where(PendingCompletion.id == row.id)
            .values(pending=pending, expires_at=datetime.now(timezone.utc) - timedelta(seconds=5))
        )
        await s.commit()

    assert await deferred_completions.expire_due() >= 1
    await worker.drain()
    (job,) = worker.jobs
    assert job["channel_id"] == row.channel_id and job["user_token"] == ""
    transcript = await _transcript(chat_id)
    assert json.loads(transcript[-2].content)["timed_out"] is True
    assert transcript[-1].content == "No answer; assuming eu."


async def test_the_resume_acts_through_the_key_the_inline_round_ran_under(env, worker):
    """#223: whoever completes the round, its continuation acts through the
    API key of the request that ran it — inline rounds record it too."""
    user, agent = env["user"], await env["make_agent"]()
    async with AsyncSessionLocal() as s:
        chat = Chat(user_id=user.id, agent_id=agent.id, agent_namespace=agent.namespace,
                    agent_name=agent.name, title="key")
        s.add(chat)
        await s.commit()
        chat_id = str(chat.id)
    script(chat_id, ask("Which?"), say("ok"))

    key_token = bind_api_key("11111111-1111-1111-1111-111111111111")
    try:
        async with AsyncSessionLocal() as s:
            turn = await ms.MessageService(s).run_turn(chat_id, str(user.id), "tok", "go")
    finally:
        reset_api_key(key_token)
    assert turn.status == "input_required"
    row = await _checkpoint(chat_id)
    assert row.conversation_context["api_key_id"] == "11111111-1111-1111-1111-111111111111"

    async with AsyncSessionLocal() as s:
        (question,) = await sync_turns.open_questions(s, chat_id)
        assert current_api_key_id() is None  # answered by a plain session
        await sync_turns.answer_question(s, chat_id, question["tool_call_id"], "this", user_token="tok")
    # The job ran without the key's permissions behind it in this test
    # (the key does not exist); what matters is what it was queued under.
    assert worker.jobs[0]["api_key_id"] == "11111111-1111-1111-1111-111111111111"
    # Let the job end, however it ends, before the test does: left running,
    # it wrote into the chat during teardown (FK violation on cleanup) and on
    # into the next test's closed event loop.
    await asyncio.wait_for(asyncio.gather(*worker.tasks, return_exceptions=True), 15)
    worker.tasks.clear()


async def test_suspend_mode_delegation_keeps_blocking_in_an_inline_turn(db, test_user, monkeypatch):
    """Suspend mode frees the worker slot a waiting parent holds; an inline
    turn holds none, so it keeps waiting for the child and a synchronous
    caller still gets the reply in the same response."""
    from app.services.delegation import current_channel_id

    monkeypatch.setattr(settings, "agent_delegate_mode", "suspend")
    ran = []

    async def fake_execute(tc, *args, **kwargs):
        ran.append(tc["id"])
        return tc["id"], tc["function"]["name"], json.dumps({"response": "child says hi"})

    monkeypatch.setattr(ms, "execute_single_tool", fake_execute)

    async def no_followup(self, **kwargs):
        if False:
            yield {}

    monkeypatch.setattr(ms.MessageService, "_stream_followup_after_tools", no_followup)

    chat = Chat(user_id=test_user.id, title="delegating")
    db.add(chat)
    await db.flush()
    delegate_tool = {
        "type": "function",
        "function": {"name": "call_agent_x__y", "parameters": {}, "_metadata": {"agent_id": str(uuid.uuid4())}},
    }
    call = {"id": "call_d", "type": "function", "function": {"name": "call_agent_x__y", "arguments": "{}"}}
    assert current_channel_id.get() is None
    chunks = [c async for c in ms.MessageService(db)._handle_tool_calls(
        chat_id=str(chat.id), user_id=str(test_user.id), user_token="t", messages=[],
        tool_calls=[call], provider=None, model=None, temperature=0.7, max_tokens=None,
        tools=[delegate_tool], permissions={},
    )]
    assert ran == ["call_d"]
    assert not any(c.get("type") == "round_suspended" for c in chunks)
    assert (await db.execute(select(PendingCompletion).where(PendingCompletion.chat_id == chat.id))).first() is None


async def test_a_queued_round_parks_on_its_job_channel(db, test_user):
    """No regression for keep-alive: the round's channel is the job's, and
    the chat's reconnect channel is the job's to manage."""
    from app.services.ask_user_tools import get_ask_user_tool_definition
    from app.services.delegation import current_channel_id

    chat = Chat(user_id=test_user.id, title="queued", active_channel_id="job-chan")
    db.add(chat)
    await db.flush()
    token = current_channel_id.set("job-chan")
    try:
        chunks = [c async for c in ms.MessageService(db)._handle_tool_calls(
            chat_id=str(chat.id), user_id=str(test_user.id), user_token="t", messages=[],
            tool_calls=[{"id": "q1", "type": "function",
                         "function": {"name": "ask_user", "arguments": json.dumps({"question": "?"})}}],
            provider=None, model=None, temperature=0.7, max_tokens=None,
            tools=[get_ask_user_tool_definition()], permissions={},
        )]
    finally:
        current_channel_id.reset(token)
    suspended = next(c for c in chunks if c.get("type") == "round_suspended")
    assert suspended["channel_id"] == "job-chan"
    row = (await db.execute(select(PendingCompletion).where(PendingCompletion.chat_id == chat.id))).scalar_one()
    assert row.channel_id == "job-chan"


# ---------------------------------------------------------------------------
# Following a channel (sync_turns)
# ---------------------------------------------------------------------------


def _usage(calls: int, cost: Optional[float] = 0.0) -> dict:
    return {"calls": calls, "prompt_tokens": 10 * calls, "completion_tokens": calls,
            "total_tokens": 11 * calls, "cache_read_tokens": 0, "cache_write_tokens": 0,
            "cost_usd": cost, "unpriced_calls": 0 if cost is not None else calls, "errors": 0}


def test_usage_adds_up_across_the_runs_of_a_turn():
    total = sync_turns.combine_usage([_usage(1, 0.5), _usage(2, None), _usage(1, 0.25)])
    assert total["calls"] == 4 and total["total_tokens"] == 44
    assert total["cost_usd"] == pytest.approx(0.75) and total["unpriced_calls"] == 2
    assert sync_turns.combine_usage([_usage(1, None)])["cost_usd"] is None
    assert sync_turns.combine_usage([])["cost_usd"] == 0.0
    # A run that reported nothing makes the total unknown, not smaller.
    assert sync_turns.combine_usage([_usage(1), None]) is None


def _fake_channel(monkeypatch, events: list[dict]):
    async def subscribe(channel_id, last_id="0", timeout=None):
        for e in events:
            yield e
        await asyncio.sleep(3600)

    monkeypatch.setattr(stream_relay, "subscribe", subscribe)


async def test_following_goes_on_past_a_background_suspension(monkeypatch):
    _fake_channel(monkeypatch, [
        {"type": "round_suspended", "tool_call_ids": ["bg2"], "usage": _usage(1)},
        {"type": "tool_end", "tool_call_id": "bg2"},
        {"content": "done!"},
        {"type": "done", "status": "completed", "usage": _usage(1)},
    ])
    f = await sync_turns.RoundFollower("c", timeout=5).run()
    assert f.status == "completed" and len(f.usages) == 2


async def test_following_stops_where_the_round_waits_on_the_user(monkeypatch):
    _fake_channel(monkeypatch, [
        {"type": "input_required", "tool_call_id": "q"},
        {"type": "round_suspended", "tool_call_ids": ["q", "bg"], "usage": _usage(1)},
    ])
    f = await asyncio.wait_for(sync_turns.RoundFollower("c", timeout=30).run(), timeout=5)
    assert f.status == "input_required"


async def test_following_reports_a_failed_continuation_and_runs_out_of_time(monkeypatch):
    _fake_channel(monkeypatch, [{"type": "error", "error": "boom"}])
    f = await sync_turns.RoundFollower("c", timeout=5).run()
    assert (f.status, f.error) == ("failed", "boom")

    # Cut off mid-run: the calls it made so far are unreported, so the
    # request's usage is unknown rather than too low.
    _fake_channel(monkeypatch, [{"type": "tool_start"}])
    f = await sync_turns.RoundFollower("c", timeout=0.3).run()
    assert f.status == "in_progress" and f.usages == [None]
    assert sync_turns.combine_usage([_usage(1), *f.usages]) is None

    # Nothing on the channel yet: a queued continuation may still be inside
    # its first model call, so that is unknown too.
    _fake_channel(monkeypatch, [])
    f = await sync_turns.RoundFollower("c", timeout=0.3).run()
    assert f.status == "in_progress" and f.usages == [None]


async def test_following_moves_with_the_round(monkeypatch):
    channels = {
        "old": [{"type": "tool_end"}, {"type": "round_moved", "channel_id": "new"}],
        "new": [{"content": "hi"}, {"type": "done", "usage": _usage(1)}],
    }

    async def subscribe(channel_id, last_id="0", timeout=None):
        for e in channels[channel_id]:
            yield e
        await asyncio.sleep(3600)

    monkeypatch.setattr(stream_relay, "subscribe", subscribe)
    f = await sync_turns.RoundFollower("old", timeout=5, last_id="5-0").run()
    assert f.status == "completed" and f.usages == [_usage(1)]


# ---------------------------------------------------------------------------
# OpenAI adapter, pipelines
# ---------------------------------------------------------------------------


async def test_openai_adapter_shows_the_question_and_takes_the_next_message_as_answer(env, http, worker):
    user, agent = env["user"], await env["make_agent"]()
    model = f"{agent.namespace}/{agent.name}"

    first_chat: dict[str, str] = {}
    original = ScriptedLLM._next

    def keyed(self, messages):  # the adapter creates the chat: script on sight
        if self._chat_id not in ScriptedLLM.scripts and not first_chat:
            first_chat["id"] = self._chat_id
            script(self._chat_id, ask("Which region?", ["eu", "us"]), say("Deploying to eu."))
        return original(self, messages)

    ScriptedLLM._next = keyed
    try:
        resp = await http.post(
            "/adapters/openai/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": "deploy it"}]},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["X-Sinas-Turn-Status"] == "input_required"
        content = resp.json()["choices"][0]["message"]["content"]
        assert "Which region?" in content and "eu / us" in content
        chat_id = resp.headers["X-Chat-ID"]

        resp = await http.post(
            "/adapters/openai/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": "eu"}]},
            headers={**auth_headers(user), "X-Chat-ID": chat_id},
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["X-Sinas-Turn-Status"] == "completed"
        assert resp.json()["choices"][0]["message"]["content"] == "Deploying to eu."
    finally:
        ScriptedLLM._next = original
    tool_row = next(m for m in await _transcript(chat_id) if m.role == "tool")
    assert json.loads(tool_row.content) == {"answer": "eu"}


async def test_a_sync_webhook_says_the_turn_waits_on_the_owner(env, http, worker):
    from app.models.webhook import Webhook

    user, agent = env["user"], await env["make_agent"]()
    path = f"inline-defer/{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as s:
        s.add(Webhook(
            user_id=user.id, path=path, target_type="agent", function_namespace="default",
            function_name=None, agent_namespace=agent.namespace, agent_name=agent.name,
            message_template="Issue {{ key }}", session_key_template="hook-{{ key }}",
            http_method="POST", requires_auth=False, response_mode="sync",
        ))
        await s.commit()
    chat_id = await _session_chat(user, agent, "hook-AB-1")
    script(chat_id, ask("Close it as a duplicate?", ["yes", "no"]))

    resp = await http.post(f"/webhooks/{path}", json={"key": "AB-1"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["chat_id"] == chat_id and out["status"] == "input_required" and out["reply"] == ""
    assert [q["question"] for q in out["pending_inputs"]] == ["Close it as a duplicate?"]


async def test_a_pipeline_agent_step_fails_clearly_on_a_question(env, monkeypatch):
    from app.services import pipeline_runner

    agent = await env["make_agent"]()

    async def enqueue(**kwargs):
        return "job"

    monkeypatch.setattr(queue_service, "enqueue_agent_message", enqueue)
    _fake_channel(monkeypatch, [
        {"type": "tool_start", "name": "ask_user"},
        {"type": "input_required", "tool_call_id": "q", "question": "Which region?"},
        {"type": "round_suspended", "tool_call_ids": ["q"]},
    ])
    with pytest.raises(pipeline_runner.StepError, match="Which region"):
        await asyncio.wait_for(
            pipeline_runner._execute_agent_step(
                {"agent": f"{agent.namespace}/{agent.name}", "message": "go"}, {},
                user_id=str(env["user"].id), user_token="t", run_id="r", agent_depth=1,
                pipeline_label="p", summary={},
            ),
            timeout=5,
        )
