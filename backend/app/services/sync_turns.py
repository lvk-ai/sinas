"""Turns for synchronous callers: one request, one result.

The non-streaming entry points — `POST /chats/{id}/messages`, the agent
invoke, a sync webhook, the OpenAI adapter, and an answer posted with
`wait: true` — return a turn's outcome in one response. A turn can park its
tool round on deferred calls (ADR 2026-09-02, and for inline turns ADR
2026-10-20): what such a caller gets back depends on what the round waits
for.

- A human (ask_user): status "input_required" with the open questions; the
  caller answers through `POST /chats/{id}/pending-input/{tool_call_id}`.
- Work that lands by itself (a background run the model waits for): the
  continuation runs in a worker and streams on the round's channel. The
  caller follows that channel within its time budget — so a synchronous
  caller still gets the final reply — and gets status "in_progress" with
  the channel when the budget runs out first.

`settle_turn` does that follow-through and assembles the outcome; the LLM
usage of the continuations is reported by the resume job on the event that
ends each one (see agent_jobs.execute_agent_delegate_resume_job) and added
to the request's own.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select

from app.models.chat import Message
from app.services import deferred_completions
from app.services.deferred_completions import ROUND_MOVED, ROUND_SUSPENDED
from app.services.message_service import (
    TURN_COMPLETED,
    TURN_IN_PROGRESS,
    TURN_INJECTED,
    TURN_INPUT_REQUIRED,
)
from app.services.stream_relay import stream_relay

logger = logging.getLogger(__name__)

_TOKEN_FIELDS = (
    "calls",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "unpriced_calls",
    "errors",
)


class TurnFailed(Exception):
    """The continuation of a parked round failed in its worker."""


class AnswerNotFound(Exception):
    """No open question with that tool_call_id in this chat."""


class AnswerConflict(Exception):
    """The question was resolved meanwhile (double submit, expiry)."""


def combine_usage(parts: list[Optional[dict[str, Any]]]) -> Optional[dict[str, Any]]:
    """Add up the usage totals of the runs a turn was made of (the request's
    own calls, then each continuation's). None when any part is unknown —
    a total that leaves out calls that were made would under-report."""
    if any(p is None for p in parts):
        return None
    total: dict[str, Any] = dict.fromkeys(_TOKEN_FIELDS, 0)
    cost = None
    for part in parts:
        for name in _TOKEN_FIELDS:
            total[name] += int(part.get(name) or 0)
        if part.get("cost_usd") is not None:
            cost = (cost or 0.0) + float(part["cost_usd"])
    # As for one run: 0 when nothing was called, None when calls were made
    # but none could be priced.
    total["cost_usd"] = cost if cost is not None else (None if total["calls"] else 0.0)
    return total


class RoundFollower:
    """Follow a parked round's continuation on its stream channel until the
    turn completes, fails, or parks on a human again — or the time budget
    runs out (status stays "in_progress").

    A round that parks again on work that lands by itself (another
    background run) resumes on the same channel, so following goes on.
    `events()` yields every event but pings, for a caller that relays them.
    """

    def __init__(self, channel_id: str, *, timeout: float, last_id: str = "0"):
        self.channel_id = channel_id
        self.timeout = timeout
        self.last_id = last_id
        self.status = TURN_IN_PROGRESS
        self.error: Optional[str] = None
        # The `error` event the turn failed with (with the error envelope's
        # `code` when it has one: budget_exceeded, provider_unavailable, ...).
        self.error_event: Optional[dict[str, Any]] = None
        # One entry per continuation run that ended on the channel: its
        # reported usage, or None when it reported none.
        self.usages: list[Optional[dict[str, Any]]] = []

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(self.timeout, 0)
        asked: set[str] = set()
        channel_id, last_id = self.channel_id, self.last_id
        while channel_id:
            moved_to = None
            stream = stream_relay.subscribe(
                channel_id, last_id=last_id, timeout=max(int(self.timeout), 1)
            ).__aiter__()
            try:
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    try:
                        event = await asyncio.wait_for(stream.__anext__(), remaining)
                    except (StopAsyncIteration, asyncio.TimeoutError):
                        break  # idle past the budget: still in progress
                    kind = event.get("type")
                    if kind == "ping":
                        continue
                    if kind == ROUND_MOVED:
                        # Resumed on another channel: follow it there.
                        moved_to = event.get("channel_id")
                        if moved_to:
                            break
                        continue
                    yield event
                    if kind == "input_required" and event.get("tool_call_id"):
                        asked.add(event["tool_call_id"])
                    elif kind == ROUND_SUSPENDED:
                        self.usages.append(event.get("usage"))
                        if asked & set(event.get("tool_call_ids") or []):
                            self.status = TURN_INPUT_REQUIRED
                            return
                    elif kind == "done":
                        self.usages.append(event.get("usage"))
                        self.status = TURN_COMPLETED
                        return
                    elif kind == "error":
                        self.status = "failed"
                        self.error = event.get("error") or "The agent's turn failed"
                        self.error_event = event
                        return
            finally:
                await stream.aclose()
            channel_id, last_id = moved_to, "0"
        # Still in progress when the budget ran out: a continuation may be
        # running (or have been queued and started its first model call
        # without emitting anything yet), so what it spent is unknown and so
        # is the request's total — reported as null, never too low.
        self.usages.append(None)

    async def run(self) -> RoundFollower:
        async for _ in self.events():
            pass
        return self


@dataclass
class TurnOutcome:
    """A turn's result as the synchronous endpoints report it."""

    status: str
    message: Optional[Message]
    reply: str
    channel_id: Optional[str] = None
    pending_inputs: list[dict[str, Any]] = field(default_factory=list)
    usage: Optional[dict[str, Any]] = None


async def _latest_assistant_message(db, chat_id: str) -> Optional[Message]:
    return (
        await db.execute(
            select(Message)
            .where(Message.chat_id == uuid.UUID(str(chat_id)), Message.role == "assistant")
            .order_by(Message.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def open_questions(db, chat_id: str) -> list[dict[str, Any]]:
    """The chat's open ask_user questions, as the API lists them."""
    return [
        {k: v for k, v in item.items() if k != "pending_completion_id"}
        for item in await deferred_completions.list_pending_inputs(db, chat_id)
    ]


async def settle_turn(
    db,
    chat_id: str,
    *,
    status: str,
    message: Optional[Message],
    channel_id: Optional[str],
    usage_parts: list[Optional[dict[str, Any]]],
    budget: float,
    follow_from: str = "0",
) -> TurnOutcome:
    """Complete a synchronous turn's result: follow an "in_progress" round
    on `channel_id` (from `follow_from`) within `budget` seconds, then read
    what the turn ended in. `usage_parts`: the usage of the request's own
    calls (empty when it made none). Raises the continuation's LLMError
    when it failed with a code, else TurnFailed."""
    if status == TURN_IN_PROGRESS and channel_id:
        follower = await RoundFollower(channel_id, timeout=budget, last_id=follow_from).run()
        usage_parts = [*usage_parts, *follower.usages]
        if follower.status == "failed":
            from app.providers.errors import LLMError, error_code

            if error_code(follower.error_event or {}):
                # A failure with a code (budget_exceeded, rate_limited,
                # provider_unavailable, ...): the API answers it with its
                # status and the error envelope.
                raise LLMError.from_event(follower.error_event)
            raise TurnFailed(follower.error)
        status = follower.status
        if status != TURN_IN_PROGRESS:
            message = await _latest_assistant_message(db, chat_id) or message

    pending_inputs: list[dict[str, Any]] = []
    if status in (TURN_INPUT_REQUIRED, TURN_INJECTED):
        # Injected: what the parked round the message went to waits on.
        pending_inputs = await open_questions(db, chat_id)
    if status == TURN_INPUT_REQUIRED and not pending_inputs:
        # Answered or expired meanwhile: the round goes on by itself.
        status = TURN_IN_PROGRESS

    if status == TURN_INJECTED:
        reply = ""  # the message was delivered, not answered
    else:
        reply = (message.content if message is not None else None) or ""
    return TurnOutcome(
        status=status,
        message=message,
        reply=reply,
        # What to follow: a turn still going on by itself, or the parked
        # round an injected message went to. A question's answer returns
        # the channel the round resumes on.
        channel_id=channel_id if status in (TURN_IN_PROGRESS, TURN_INJECTED) else None,
        pending_inputs=pending_inputs,
        usage=combine_usage(usage_parts),
    )


@dataclass
class Answered:
    """The effect of answering an open question (see answer_question)."""

    tool_call_id: str
    resumed: bool  # the answer was the round's last completion; resume queued
    resume_pending: bool  # last completion, resume not queued yet (retried)
    channel_id: Optional[str]  # where the conversation continues
    follow_from: str  # stream id a waiting caller follows channel_id from


async def answer_question(
    db, chat_id: str, tool_call_id: str, answer: str, *, user_token: str
) -> Answered:
    """Record the answer to an open ask_user question as its tool result.

    When it is the round's last outstanding completion, the round resumes
    on a fresh channel (a delegated child whose parent waits on its original
    channel resumes there); otherwise the round keeps waiting for the rest
    and `channel_id` is the round's own channel, where it resumes when its
    other completions land. Raises AnswerNotFound / AnswerConflict.
    """
    from app.models.pending_completion import PendingCompletion

    pending = await deferred_completions.list_pending_inputs(db, chat_id)
    match = next((p for p in pending if p["tool_call_id"] == tool_call_id), None)
    if not match:
        raise AnswerNotFound(tool_call_id)

    # Where the round streams now, and how far: a caller waiting on a round
    # that resumes there must not mistake its history for its continuation.
    round_channel = (
        await db.execute(
            select(PendingCompletion.channel_id).where(
                PendingCompletion.id == uuid.UUID(match["pending_completion_id"])
            )
        )
    ).scalar_one_or_none()
    round_tail = await stream_relay.last_entry_id(round_channel) if round_channel else "0"

    fresh_channel = str(uuid.uuid4())
    outcome = await deferred_completions.complete(
        match["pending_completion_id"],
        tool_call_id,
        json.dumps({"answer": answer}),
        user_token=user_token,
        resume_channel_id=fresh_channel,
    )
    if outcome["status"] != "completed":
        raise AnswerConflict(tool_call_id)

    resumed = bool(outcome.get("resumed"))
    resume_pending = bool(outcome.get("resume_pending"))
    if resumed or resume_pending:
        channel = outcome.get("channel_id") or fresh_channel
        follow_from = "0" if channel == fresh_channel else round_tail
    else:
        channel, follow_from = round_channel, round_tail
    return Answered(
        tool_call_id=tool_call_id,
        resumed=resumed,
        resume_pending=resume_pending,
        channel_id=channel,
        follow_from=follow_from,
    )


async def settle_answer(
    db, chat_id: str, answered: Answered, *, budget: float
) -> TurnOutcome:
    """What a caller waiting on its answer gets: the resumed turn's result
    (followed within `budget`), or — when other completions are still
    outstanding — what the round still waits for."""
    if answered.resumed or answered.resume_pending:
        status = TURN_IN_PROGRESS
    elif await open_questions(db, chat_id):
        status = TURN_INPUT_REQUIRED
    else:
        status = TURN_IN_PROGRESS  # the rest lands by itself: follow it
    return await settle_turn(
        db,
        chat_id,
        status=status,
        message=None,
        channel_id=answered.channel_id,
        usage_parts=[],
        budget=budget,
        follow_from=answered.follow_from,
    )
