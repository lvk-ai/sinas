"""MCP tasks kept beyond the inline wait (ADR 2026-10-12-mcp-tasks).

An MCP server may answer a tool call with a task handle instead of a result
(the `io.modelcontextprotocol/tasks` extension, MCP 2026-07-28).
`mcp_client.call_tool` polls it inline for a short while; a task still
running after that, or waiting for input, comes here:

- `keep_task` stores it (`mcp_tasks`) and gives the agent a handle;
- the agent worker sweeps due tasks every few seconds (`poll_due_tasks`),
  calling `tasks/get` at the server's suggested interval;
- a finished task's result is delivered into the chat, like a finished
  background job (`chat_wakeups.deliver_to_chat`); an input request is
  delivered once, for the agent to answer with `mcp_task_respond`;
- `mcp_task_status` and `mcp_task_cancel` let the agent look and stop.

The agent only ever sees this row's id: the server's own task id may double
as a bearer token for its state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select

from app.core.config import settings
from app.models.chat import Chat
from app.models.mcp_server import McpServer
from app.models.mcp_task import McpTask
from app.services import mcp_client
from app.services.mcp_client import TERMINAL_TASK_STATUSES, McpClientError, TaskState

logger = logging.getLogger(__name__)

MCP_TASK_STATUS = "mcp_task_status"
MCP_TASK_RESPOND = "mcp_task_respond"
MCP_TASK_CANCEL = "mcp_task_cancel"
MCP_TASK_TOOL_NAMES = frozenset({MCP_TASK_STATUS, MCP_TASK_RESPOND, MCP_TASK_CANCEL})

# How long a sweep holds a task it is polling, so two sweeps (or a crashed
# one) never poll it twice at once and it is retried if one dies.
_LEASE = timedelta(minutes=2)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _interval(task: TaskState) -> timedelta:
    seconds = mcp_client.poll_interval_seconds(
        task,
        floor=settings.mcp_task_min_poll_seconds,
        ceiling=settings.mcp_task_max_poll_seconds,
        default=settings.mcp_task_default_poll_seconds,
    )
    return timedelta(seconds=seconds)


def _label(row: McpTask, server: Optional[McpServer]) -> str:
    where = f"{server.namespace}/{server.name}" if server is not None else "mcp"
    return f"{where}/{row.tool_name}"


def _outstanding(row: McpTask) -> dict[str, Any]:
    """Input requests the server is waiting on that have not been answered."""
    answered = set(row.answered_input_keys or [])
    return {k: v for k, v in (row.input_requests or {}).items() if k not in answered}


def _clip(payload: Any, task_id: str) -> str:
    content = json.dumps(payload, default=str)
    limit = settings.background_job_result_max_chars
    if len(content) <= limit:
        return content
    return json.dumps({
        "truncated": True,
        "result_preview": content[:limit],
        "hint": f"Result clipped ({len(content) - limit} more chars); the full result is "
                f"available via {MCP_TASK_STATUS}('{task_id}').",
    })


# ---------------------------------------------------------------------------
# Keeping a task
# ---------------------------------------------------------------------------

async def keep_task(
    db,
    *,
    chat: Chat,
    user_id: str,
    server: McpServer,
    task: TaskState,
    tool_name: str,
    model_tool_name: str,
    tool_call_id: Optional[str],
) -> dict[str, Any]:
    """Store a task that outlived the inline wait; returns the tool result
    the agent gets in place of the tool's own."""
    from app.core.auth import current_api_key_id

    now = _now()
    lifetime = (
        timedelta(milliseconds=task.ttl_ms) if task.ttl_ms
        else timedelta(seconds=settings.mcp_task_max_age_seconds)
    )
    waiting_for_input = task.status == "input_required"
    row = McpTask(
        chat_id=chat.id,
        user_id=uuid.UUID(str(user_id)),
        mcp_server_id=server.id,
        server_task_id=task.task_id,
        tool_name=tool_name,
        model_tool_name=model_tool_name,
        tool_call_id=tool_call_id,
        status=task.status,
        status_message=task.status_message,
        poll_interval_ms=task.poll_interval_ms,
        next_poll_at=now + _interval(task),
        expires_at=now + lifetime,
        input_requests=task.input_requests if waiting_for_input else None,
        # Requests returned in this tool result are already in front of the
        # agent: the poller must not deliver them a second time.
        delivered_input_keys=list(task.input_requests or {}) if waiting_for_input else [],
        answered_input_keys=[],
        pending_deliveries=[],
        api_key_id=current_api_key_id(),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    out: dict[str, Any] = {"task_id": str(row.id), "status": row.status}
    if row.status_message:
        out["status_message"] = row.status_message
    if waiting_for_input:
        out["input_requests"] = task.input_requests
        out["hint"] = (
            f"The MCP server is running this as a task and needs input before it can go on. "
            f"Answer with {MCP_TASK_RESPOND}(task_id, responses), one response per request key; "
            f"when a request asks the user for something, ask the user first. Its result is "
            f"delivered into this chat as a message prefixed [mcp task {row.id} ...]."
        )
    else:
        out["hint"] = (
            f"The MCP server is running this as a task. Its result is delivered into this chat "
            f"as a message prefixed [mcp task {row.id} ...] when it finishes: carry on with other "
            f"work or end your turn. Check it with {MCP_TASK_STATUS}, stop it with {MCP_TASK_CANCEL}."
        )
    return out


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

async def poll_due_tasks(now: Optional[datetime] = None, limit: int = 50) -> int:
    """Poll every task whose time has come, and retry deliveries that did not
    get through. The agent worker runs this every few seconds; returns how
    many tasks it handled.

    Each task is claimed when its own poll starts (not when the sweep picks
    it): a lease taken up front could run out while the task still waits for
    a polling slot, and another sweep would then poll it too."""
    from sqlalchemy import update

    from app.core.database import AsyncSessionLocal

    now = now or _now()
    async with AsyncSessionLocal() as db:
        ids = (
            await db.execute(
                select(McpTask.id)
                .where(McpTask.next_poll_at <= now)
                .order_by(McpTask.next_poll_at)
                .limit(limit)
            )
        ).scalars().all()

    gate = asyncio.Semaphore(max(1, settings.mcp_task_poll_concurrency))
    handled = 0

    async def one(task_id: uuid.UUID) -> None:
        nonlocal handled
        async with gate:
            async with AsyncSessionLocal() as db:
                claimed = (
                    await db.execute(
                        update(McpTask)
                        .where(McpTask.id == task_id, McpTask.next_poll_at <= now)
                        # Claimed until past the sweep's own "now" too, so a
                        # sweep running at that time can't take it as well;
                        # the poll sets the real next time.
                        .values(next_poll_at=max(now, _now()) + _LEASE)
                        .returning(McpTask.id)
                    )
                ).scalar_one_or_none()
                await db.commit()
            if claimed is None:
                return  # another sweep took it, or it is no longer due
            handled += 1
            try:
                await poll_task(task_id)
            except Exception:
                logger.exception("Polling MCP task %s failed", task_id)

    await asyncio.gather(*(one(i) for i in ids))
    return handled


async def poll_task(
    task_id: uuid.UUID, *, deliver: bool = True, as_caller: bool = False
) -> Optional[dict[str, Any]]:
    """One `tasks/get` for a kept task, and whatever follows from it, then
    the delivery of anything it produced. With `deliver=False` (the status
    tool) nothing is delivered into the chat: the caller hands the snapshot
    to the agent itself. Returns the snapshot.

    A background poll acts through the API key the calling turn acted
    through, so the server access check holds it to that key's grants, as it
    held the call. With `as_caller` (the agent's status tool) it acts as
    whoever is calling now instead: a later turn on a narrower key must not
    reach the server through the key that created the task."""
    from app.core.auth import bind_api_key, reset_api_key
    from app.core.database import AsyncSessionLocal

    if as_caller:
        snap = await _poll_task(task_id, deliver=deliver)
        if deliver:
            await _flush_deliveries(task_id)
        return snap

    async with AsyncSessionLocal() as db:
        row = await db.get(McpTask, task_id)
        api_key_id = row.api_key_id if row is not None else None
    key_token = bind_api_key(api_key_id)
    try:
        snap = await _poll_task(task_id, deliver=deliver)
        if deliver:
            await _flush_deliveries(task_id)
        return snap
    finally:
        reset_api_key(key_token)


async def _poll_task(task_id: uuid.UUID, *, deliver: bool) -> Optional[dict[str, Any]]:
    from app.core.database import AsyncSessionLocal
    from app.services.mcp_tools import accessible_server

    async with AsyncSessionLocal() as db:
        row = await db.get(McpTask, task_id)
        if row is None:
            return None
        if row.finished_at is not None:
            return snapshot(row)
        server = await db.get(McpServer, row.mcp_server_id)
        user_id = str(row.user_id)

        if row.expires_at is not None and _now() >= row.expires_at:
            return await _finish(
                db, row, server, "failed",
                {"error": "The task expired before it finished (the server may have dropped it)."},
                deliver=deliver,
            )
        usable, problem = (
            await accessible_server(db, server.namespace, server.name, user_id) if server is not None
            else (None, "The MCP server was removed")
        )
        if usable is None:
            return await _finish(db, row, server, "failed", {"error": problem}, deliver=deliver)

        task: Optional[TaskState] = None
        error: Optional[McpClientError] = None
        try:
            task = await mcp_client.get_task(db, usable, user_id, row.server_task_id)
        except McpClientError as e:
            error = e
        result = None
        if task is not None and task.status in TERMINAL_TASK_STATUSES:
            # Mapped before the row is locked: storing binary content
            # commits, which would release the lock.
            result = await _map_result(db, row, task)

        # The row may have changed while the server answered (a cancel, a
        # response, another poll): decide on what it holds now.
        await db.refresh(row, with_for_update=True)
        if row.finished_at is not None:
            await db.commit()
            return snapshot(row)
        now = _now()

        if error is not None:
            row.failures += 1
            if row.failures >= settings.mcp_task_max_poll_failures:
                return await _finish(
                    db, row, usable, "failed",
                    {"error": f"Lost contact with the MCP server while the task ran: {error}"},
                    deliver=deliver,
                )
            backoff = min(settings.mcp_task_max_poll_seconds, settings.mcp_task_min_poll_seconds * 2 ** row.failures)
            row.next_poll_at = now + timedelta(seconds=backoff)
            await db.commit()
            logger.info("MCP task %s: poll failed (%s), retrying in %ss", row.id, error, backoff)
            return snapshot(row)

        if result is not None:
            return await _finish(
                db, row, usable, task.status, result, deliver=deliver, status_message=task.status_message
            )

        row.failures = 0
        row.status = task.status
        row.status_message = task.status_message
        row.poll_interval_ms = task.poll_interval_ms
        row.next_poll_at = now + _interval(task)
        if task.status == "input_required":
            row.input_requests = task.input_requests or {}
            seen = set(row.delivered_input_keys or []) | set(row.answered_input_keys or [])
            new_requests = {k: v for k, v in row.input_requests.items() if k not in seen}
            if new_requests and deliver:
                # Recorded with the state that produced it: if delivery
                # fails, the sweep retries it; it is never silently lost.
                row.delivered_input_keys = list(row.delivered_input_keys or []) + list(new_requests)
                _queue_delivery(
                    row,
                    f"[mcp task {row.id} needs input: {_label(row, usable)}]\n"
                    f"{json.dumps(new_requests, default=str)}\n"
                    f"Answer with {MCP_TASK_RESPOND}('{row.id}', responses), one response per request "
                    f"key; when a request asks the user for something, ask the user first.",
                )
        await db.commit()
        return snapshot(row)


async def _map_result(db, row: McpTask, task: TaskState) -> dict[str, Any]:
    """The finished task's result, mapped like any MCP tool result (binary
    content goes to the chat's workbench when it has one)."""
    from app.services.mcp_tools import McpToolConverter

    store_blob = None
    chat = await db.get(Chat, row.chat_id)
    if chat is not None:
        store_blob = await McpToolConverter()._blob_store(db, chat, str(row.user_id), row.model_tool_name)
    try:
        return await mcp_client.map_call_result(
            mcp_client.task_outcome(task),
            tool_name=row.tool_name,
            store_blob=store_blob,
            blob_prefix=mcp_client.blob_prefix_for(row.model_tool_name, row.tool_call_id),
        )
    except Exception as e:  # a malformed result is still an answer
        logger.warning("MCP task %s: could not map its result: %s", row.id, e)
        return {"error": f"The task finished but its result could not be read: {e}"}


async def _finish(
    db, row: McpTask, server: Optional[McpServer], status: str, result: dict[str, Any], *,
    deliver: bool, status_message: Optional[str] = None,
) -> dict[str, Any]:
    """Mark the task finished, once: whoever sets finished_at under the row
    lock queues the delivery; anyone else (a concurrent sweep, a status
    call, a cancel) doesn't."""
    await db.refresh(row, with_for_update=True)
    if row.finished_at is not None:
        await db.commit()
        return snapshot(row)
    row.status = status
    if status_message is not None:
        row.status_message = status_message
    row.result = result
    row.finished_at = _now()
    row.next_poll_at = None
    if deliver:
        _queue_delivery(row, f"[mcp task {row.id} {status}: {_label(row, server)}]\n{_clip(result, str(row.id))}")
    await db.commit()
    return snapshot(row)


def _queue_delivery(row: McpTask, content: str) -> None:
    """Add a message to the task's outbox, in the same commit as the state
    change it reports. `_flush_deliveries` sends it; until it has, the task
    stays due, so the sweep retries."""
    entry = {"id": uuid.uuid4().hex, "content": content}
    row.pending_deliveries = list(row.pending_deliveries or []) + [entry]
    if row.next_poll_at is None:  # finished: due again until delivered
        row.next_poll_at = _now()


_DELIVERY_RETRY = timedelta(seconds=30)


async def _flush_deliveries(task_id: uuid.UUID) -> None:
    """Send the task's outbox into the chat, in order.

    Each message leaves the outbox in the same transaction that writes it
    into the chat (`deliver_to_chat`'s guard): it is delivered exactly when
    it is removed. Two flushes racing can't both send it (the second finds it
    gone), and a cancel that empties the outbox first means it is never
    sent. No lock is held across the delivery: the guard locks the chat row
    and then this row, in the order that deleting a chat takes them.

    A message the chat refuses for good (an archived chat, an inactive
    owner, a hook that blocks it) is dropped from the outbox; one whose
    delivery fails stays there for the next sweep (and a new turn is queued
    under a job id fixed by the message, so a retry never queues it twice)."""
    from app.core.database import AsyncSessionLocal
    from app.services.chat_wakeups import deliver_to_chat

    while True:
        async with AsyncSessionLocal() as db:
            row = await db.get(McpTask, task_id)
            if row is None:
                return
            pending = list(row.pending_deliveries or [])
            if not pending:
                if row.finished_at is not None and row.next_poll_at is not None:
                    row.next_poll_at = None  # nothing left to poll or deliver
                    await db.commit()
                return
            entry, chat_id, api_key_id = pending[0], row.chat_id, row.api_key_id

        async def take_off(session) -> bool:
            """In the delivery's transaction: the message is still owed, and
            now it isn't."""
            await session.execute(
                select(Chat.id).where(Chat.id == chat_id).with_for_update(key_share=True)
            )
            task = (
                await session.execute(
                    select(McpTask).where(McpTask.id == task_id).with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            current = list(task.pending_deliveries or []) if task is not None else []
            if not current or current[0].get("id") != entry["id"]:
                return False
            task.pending_deliveries = current[1:]
            if task.finished_at is not None and not task.pending_deliveries:
                task.next_poll_at = None
            return True

        try:
            outcome = await deliver_to_chat(
                str(chat_id), entry["content"], source="mcp_task", api_key_id=api_key_id,
                guard=take_off, delivery_id=entry["id"],
            )
        except Exception:
            # Never put the message back: if it left the outbox, the
            # transaction that delivered it committed. If it is still there,
            # the next sweep tries again.
            logger.exception("Delivering MCP task %s into chat %s failed; retrying", task_id, chat_id)
            await _retry_later(task_id)
            return

        if outcome.get("status") == "dropped" and outcome.get("reason") != "withdrawn":
            # Refused for good before reaching the guard: don't retry it.
            await _remove_entry(task_id, entry["id"])


async def _remove_entry(task_id: uuid.UUID, entry_id: str) -> None:
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        row = await db.get(McpTask, task_id, with_for_update=True)
        if row is None:
            return
        row.pending_deliveries = [e for e in (row.pending_deliveries or []) if e.get("id") != entry_id]
        if row.finished_at is not None and not row.pending_deliveries:
            row.next_poll_at = None
        await db.commit()


async def _retry_later(task_id: uuid.UUID) -> None:
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        row = await db.get(McpTask, task_id, with_for_update=True)
        if row is None or not row.pending_deliveries:
            await db.commit()
            return
        retry = _now() + _DELIVERY_RETRY
        if row.finished_at is not None or row.next_poll_at is None or row.next_poll_at > retry:
            row.next_poll_at = retry
        await db.commit()


def snapshot(row: McpTask) -> dict[str, Any]:
    """What the agent sees of a kept task."""
    out: dict[str, Any] = {
        "task_id": str(row.id),
        "tool": row.model_tool_name,
        "status": row.status,
        "started_at": _iso(row.created_at),
    }
    if row.status_message:
        out["status_message"] = row.status_message
    if row.status == "input_required" and row.finished_at is None:
        outstanding = _outstanding(row)
        if outstanding:
            out["input_requests"] = outstanding
    if row.finished_at is not None:
        out["finished_at"] = _iso(row.finished_at)
        out["result"] = row.result
    return out


# ---------------------------------------------------------------------------
# The agent's tools
# ---------------------------------------------------------------------------

def get_tool_definitions() -> list[dict[str, Any]]:
    """Offered whenever the agent has an MCP server bound."""
    meta = {"system_tool": True, "tool_type": "mcp_task"}
    task_id = {"type": "string", "description": "The task_id the MCP tool call returned."}
    return [
        {
            "type": "function",
            "function": {
                "name": MCP_TASK_STATUS,
                "description": (
                    "Check an MCP task: a tool call the MCP server is running in the "
                    "background. Returns its status, any input it waits for, and its "
                    "result once finished. Finished results are also delivered into this "
                    "chat on their own, so only check when you need to now."
                ),
                "parameters": {"type": "object", "properties": {"task_id": task_id}, "required": ["task_id"]},
                "_metadata": meta,
            },
        },
        {
            "type": "function",
            "function": {
                "name": MCP_TASK_RESPOND,
                "description": (
                    "Answer the input an MCP task is waiting for. `responses` maps each "
                    "request key from its input_requests to your response: for an "
                    "elicitation, {\"action\": \"accept\", \"content\": {...}} with the "
                    "requested fields, or {\"action\": \"decline\"}. When a request asks the "
                    "user for something, ask the user first."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": task_id,
                        "responses": {"type": "object", "description": "Response per input request key."},
                    },
                    "required": ["task_id", "responses"],
                },
                "_metadata": meta,
            },
        },
        {
            "type": "function",
            "function": {
                "name": MCP_TASK_CANCEL,
                "description": (
                    "Stop an MCP task you no longer need. Cancellation is a request: the "
                    "server may still finish the work, but nothing more is delivered here."
                ),
                "parameters": {"type": "object", "properties": {"task_id": task_id}, "required": ["task_id"]},
                "_metadata": meta,
            },
        },
    ]


async def _own_task(db, chat: Chat, task_id: str) -> Optional[McpTask]:
    try:
        tid = uuid.UUID(str(task_id))
    except ValueError:
        return None
    row = await db.get(McpTask, tid)
    if row is None or row.chat_id != chat.id:
        return None
    return row


async def execute_tool(db, chat: Optional[Chat], user_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from app.services.mcp_tools import accessible_server

    if chat is None:
        return {"error": "MCP task tools need a chat"}
    row = await _own_task(db, chat, str(arguments.get("task_id", "")))
    if row is None:
        return {"error": "Unknown task_id for this chat"}

    if row.finished_at is not None:
        if tool_name == MCP_TASK_STATUS:
            return snapshot(row)  # stored here; the server isn't asked
        return {"error": f"The task already finished ({row.status})", **snapshot(row)}

    # Every call that reaches the server is held to the CURRENT caller's
    # access (the turn's own API key), not the key that created the task.
    server = await db.get(McpServer, row.mcp_server_id)
    usable, problem = (
        await accessible_server(db, server.namespace, server.name, user_id) if server is not None
        else (None, "The MCP server was removed")
    )
    if usable is None:
        return {"error": problem}

    if tool_name == MCP_TASK_STATUS:
        # A live look; nothing is delivered for it, the agent has it here.
        return await poll_task(row.id, deliver=False, as_caller=True) or {"error": "Unknown task_id for this chat"}

    if tool_name == MCP_TASK_RESPOND:
        responses = arguments.get("responses")
        if not isinstance(responses, dict) or not responses:
            return {"error": "responses must be an object keyed by input request key"}
        outstanding = _outstanding(row)
        unknown = sorted(set(responses) - set(outstanding))
        if unknown:
            return {"error": f"Not waiting for: {', '.join(unknown)}", "input_requests": outstanding}
        try:
            await mcp_client.update_task(db, usable, user_id, row.server_task_id, responses)
        except McpClientError as e:
            return {"error": str(e)}
        await db.refresh(row, with_for_update=True)
        row.answered_input_keys = list(row.answered_input_keys or []) + list(responses)
        if row.finished_at is None:
            row.status = "working"
            row.next_poll_at = _now()  # look again soon
        await db.commit()
        return {"task_id": str(row.id), "status": "working",
                "hint": "Answer sent; the result is delivered into this chat when the task finishes."}

    if tool_name == MCP_TASK_CANCEL:
        try:
            await mcp_client.cancel_task(db, usable, user_id, row.server_task_id)
            note = "Cancellation sent to the server."
        except McpClientError as e:
            note = f"Could not reach the server to cancel ({e}); stopped following the task."
        await db.refresh(row, with_for_update=True)
        if row.finished_at is None:
            row.status = "cancelled"
            row.result = {"error": "Cancelled by the agent"}
            row.finished_at = _now()
            row.next_poll_at = None
            # Nothing more about this task reaches the chat, including an
            # input request still waiting to be delivered.
            row.pending_deliveries = []
        await db.commit()
        return {"task_id": str(row.id), "status": row.status, "note": note}

    return {"error": f"Unknown MCP task tool {tool_name}"}


async def list_tasks(db, chat: Chat) -> list[dict[str, Any]]:
    """The chat's unfinished tasks, for list_wakeups."""
    rows = (
        await db.execute(
            select(McpTask)
            .where(McpTask.chat_id == chat.id, McpTask.finished_at.is_(None))
            .order_by(McpTask.created_at)
        )
    ).scalars().all()
    return [snapshot(r) for r in rows]
