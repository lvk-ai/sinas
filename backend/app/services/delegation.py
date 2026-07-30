"""Agent-to-agent delegation support (issue #90).

Delegation depth is carried on the arq job (`depth` kwarg) and exposed to the
tool layer through a ContextVar set by the agent job handlers — the tool code
that enqueues a child runs deep inside the message loop and has no access to
the job's kwargs.

Depth 0 = user-initiated chat; every `call_agent_*` hop adds 1. Delegated
jobs route to the dedicated sub-agent queue (see `queue_service`) so parents
waiting on children can never starve them of worker slots, and chains are
bounded by `settings.agent_max_delegation_depth`.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextvars import ContextVar
from typing import Any

from sqlalchemy import select

from app.core.config import settings

logger = logging.getLogger(__name__)

# Set by execute_agent_message_job / execute_agent_resume_job from job kwargs.
current_delegation_depth: ContextVar[int] = ContextVar(
    "current_delegation_depth", default=0
)

# The stream channel of the job currently processing this conversation.
# Needed to suspend: the resume job must publish to the same channel the
# user is subscribed to. None when not running inside an agent job (e.g.
# direct/synchronous API paths) — suspend mode falls back to blocking there.
current_channel_id: ContextVar[str | None] = ContextVar(
    "current_channel_id", default=None
)

# Job-level fields a suspension must carry into the resume job: the batch
# Execution row to terminate, the stream TTL, and — when this job is itself
# a delegated child — the parent checkpoint to report to when done.
current_job_meta: ContextVar[dict] = ContextVar("current_job_meta", default={})


def child_depth_or_error() -> tuple[int, str | None]:
    """Depth for a would-be child delegation, or an error when over the bound."""
    depth = current_delegation_depth.get() + 1
    limit = settings.agent_max_delegation_depth
    if limit and depth > limit:
        return depth, (
            f"Delegation depth limit reached ({limit}). This agent is already "
            f"{depth - 1} delegation hop(s) deep; it cannot call another agent. "
            "Answer with the information available, or raise "
            "AGENT_MAX_DELEGATION_DEPTH if deeper chains are intended."
        )
    return depth, None


async def suspend_delegations(
    db,
    *,
    chat_id: str,
    user_id: str,
    user_token: str,
    channel_id: str,
    delegations: list[dict[str, Any]],
    conversation_context: dict[str, Any],
) -> str:
    """Persist a PendingDelegation checkpoint, then enqueue the children.

    `delegations`: [{"tool_call_id", "sub_chat_id", "agent", "content"}].
    Order matters: the row must exist before any child is enqueued, or a fast
    child could report completion against a checkpoint that isn't there yet.
    Returns the pending_delegation row id.
    """
    from app.models.pending_delegation import PendingDelegation
    from app.services.queue_service import queue_service

    row = PendingDelegation(
        chat_id=chat_id,
        user_id=user_id,
        channel_id=channel_id,
        pending={
            d["tool_call_id"]: {"sub_chat_id": d["sub_chat_id"], "agent": d["agent"]}
            for d in delegations
        },
        results={},
        remaining=len(delegations),
        conversation_context=conversation_context,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    for d in delegations:
        await queue_service.enqueue_agent_message(
            chat_id=d["sub_chat_id"],
            user_id=user_id,
            user_token=user_token,
            content=d["content"],
            channel_id=str(uuid.uuid4()),  # child's own stream channel
            agent=d["agent"],
            depth=conversation_context.get("delegation_depth", 0) + 1,
            pending_delegation_id=str(row.id),
            parent_tool_call_id=d["tool_call_id"],
        )

    logger.info(
        "Suspended chat %s on %d delegated sub-agent(s) (pending_delegation=%s)",
        chat_id, len(delegations), row.id,
    )
    return str(row.id)


async def on_child_complete(
    pending_delegation_id: str,
    tool_call_id: str,
    content: str,
    *,
    user_token: str,
) -> None:
    """Record a finished child; when it is the last one, wake the parent.

    Writes the child's final content as the parent's tool-result Message row
    (the source the follow-up LLM turn is rebuilt from), then — on the last
    child — deletes the checkpoint and enqueues the delegate-resume job.
    Concurrency-safe via SELECT ... FOR UPDATE on the checkpoint row.
    """
    from app.core.database import AsyncSessionLocal
    from app.models.chat import Message
    from app.models.pending_delegation import PendingDelegation
    from app.services.queue_service import queue_service

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PendingDelegation)
            .where(PendingDelegation.id == pending_delegation_id)
            .with_for_update()
        )
        row = result.scalar_one_or_none()
        if not row:
            logger.warning(
                "PendingDelegation %s not found for tool_call %s — parent already resumed or cleaned up",
                pending_delegation_id, tool_call_id,
            )
            return

        info = (row.pending or {}).get(tool_call_id, {})

        # Persist the tool result on the parent conversation NOW, in this
        # transaction — the resume job rebuilds messages from the DB.
        db.add(
            Message(
                chat_id=row.chat_id,
                role="tool",
                content=content,
                tool_call_id=tool_call_id,
                name=f"call_agent_{info.get('agent', '').replace('/', '__')}" if info.get("agent") else None,
            )
        )

        # JSON columns need reassignment (in-place mutation isn't tracked).
        results = dict(row.results or {})
        results[tool_call_id] = content
        row.results = results
        pending = dict(row.pending or {})
        pending.pop(tool_call_id, None)
        row.pending = pending
        row.remaining = row.remaining - 1

        is_last = row.remaining <= 0
        ctx = row.conversation_context
        chat_id, user_id, channel_id = str(row.chat_id), str(row.user_id), row.channel_id
        if is_last:
            await db.delete(row)
        await db.commit()

    # Progressive UX: close this tool call on the parent's stream now, even
    # though the conversation only continues when the last child lands.
    try:
        from app.services.stream_relay import stream_relay

        await stream_relay.publish(
            channel_id,
            {
                "type": "tool_end",
                "tool_call_id": tool_call_id,
                "name": f"call_agent_{info.get('agent', '').replace('/', '__')}",
                "result": content,
            },
        )
    except Exception:
        logger.debug("Could not publish delegate tool_end to %s", channel_id)

    if is_last:
        await queue_service.enqueue_agent_delegate_resume(
            chat_id=chat_id,
            user_id=user_id,
            user_token=user_token,
            channel_id=channel_id,
            conversation_context=ctx,
        )
        logger.info(
            "All delegations done for chat %s — resume job enqueued", chat_id
        )

