"""Sinas's own bounded retry of an agent turn's LLM calls (issue #131).

The SDKs already retry a 429 or a 5xx a couple of times, quickly, honoring a
Retry-After header. A provider that asks for a longer wait, or says so only
in its error body (Gemini's `RetryInfo.retryDelay`, also behind an
OpenAI-compatible gateway, which the SDK doesn't read), still fails the call,
and with it a turn that may have run many tool calls already. So a turn's
calls (the first one, and the follow-up after every tool round) are made
again when they fail with a code in RETRIED_CODES:

- after the wait the provider asked for (`LLMError.retry_after_seconds`),
  else an exponential backoff;
- at most LLM_RETRY_MAX_ATTEMPTS calls, and LLM_RETRY_MAX_WAIT_SECONDS of
  waiting in all (each of a turn's calls has its own budget); a wait that
  doesn't fit fails the call at once instead;
- never past the agent job's timeout: a wait that would leave the retried
  call less than JOB_DEADLINE_MARGIN seconds fails the call at once (with
  its code) rather than letting the job time out mid-wait;
- never `budget_exceeded` (or any other code): retrying can't help;
- a stream only before it showed anything (content, thinking, a tool call):
  a stream that fails part-way is not replayed, so nothing is shown twice.

While it waits the chat stays locked (the lock's heartbeat runs, and the
lock is refreshed after each wait); stream consumers get their keep-alive
pings as usual.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Optional, TypeVar

from app.core.config import settings
from app.providers.errors import PROVIDER_UNAVAILABLE, RATE_LIMITED, LLMError

logger = logging.getLogger(__name__)

T = TypeVar("T")

RETRIED_CODES = frozenset({RATE_LIMITED, PROVIDER_UNAVAILABLE})
# Backoff when the provider named no wait: 2s, 4s, 8s, ... up to 30s.
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 30.0
# What a retried call needs at least, after its wait, before the job's timeout.
JOB_DEADLINE_MARGIN = 15.0


def job_time_left() -> Optional[float]:
    """Seconds until the running agent job times out, or None outside one."""
    from app.services.chat_steering import current_job_deadline

    deadline = current_job_deadline.get()
    if deadline is None:
        return None
    return deadline - asyncio.get_running_loop().time()


@dataclass
class RetryBudget:
    """The retries one call may still make."""

    max_attempts: int
    max_wait: float
    attempts: int = 1  # calls made so far, the current one included
    waited: float = 0.0

    @classmethod
    def from_settings(cls) -> "RetryBudget":
        return cls(
            max_attempts=max(1, int(settings.llm_retry_max_attempts)),
            max_wait=max(0.0, float(settings.llm_retry_max_wait_seconds)),
        )

    def delay_for(self, error: BaseException) -> Optional[float]:
        """How long to wait before making the call again after `error`, or
        None when it must not be retried."""
        if not isinstance(error, LLMError) or error.code not in RETRIED_CODES:
            return None
        if self.attempts >= self.max_attempts:
            return None
        if error.retry_after_seconds is not None:
            delay = max(0.0, error.retry_after_seconds)
        else:
            delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (self.attempts - 1))
        if self.waited + delay > self.max_wait:
            return None
        left = job_time_left()
        if left is not None and delay + JOB_DEADLINE_MARGIN > left:
            return None
        return delay


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _wait(error: LLMError, delay: float, budget: RetryBudget, chat_id: Optional[str]) -> None:
    logger.warning(
        "LLM call failed (%s%s); retrying in %.1fs (attempt %d of %d)",
        error.code,
        f", provider '{error.provider}'" if error.provider else "",
        delay,
        budget.attempts + 1,
        budget.max_attempts,
    )
    await _sleep(delay)
    budget.waited += delay
    budget.attempts += 1
    if chat_id:
        from app.services import chat_steering

        await chat_steering.refresh_chat_lock(chat_id)


def _shows_something(chunk: Any) -> bool:
    """Whether a stream chunk carries output a consumer passes on."""
    if not isinstance(chunk, dict):
        return True
    return bool(chunk.get("content") or chunk.get("tool_calls") or chunk.get("thinking"))


async def stream_with_retry(
    open_stream: Callable[[], AsyncIterator[dict[str, Any]]],
    *,
    chat_id: Optional[str] = None,
    budget: Optional[RetryBudget] = None,
) -> AsyncIterator[dict[str, Any]]:
    """The chunks of `open_stream()`, opened again (module docstring) when it
    fails with a retried code before it showed anything."""
    budget = budget or RetryBudget.from_settings()
    while True:
        shown = False
        try:
            async with aclosing(open_stream()) as stream:
                async for chunk in stream:
                    shown = shown or _shows_something(chunk)
                    yield chunk
            return
        except LLMError as e:
            delay = None if shown else budget.delay_for(e)
            if delay is None:
                raise
            await _wait(e, delay, budget, chat_id)


async def call_with_retry(
    call: Callable[[], Awaitable[T]],
    *,
    chat_id: Optional[str] = None,
    budget: Optional[RetryBudget] = None,
) -> T:
    """`await call()`, made again (module docstring) when it fails with a
    retried code."""
    budget = budget or RetryBudget.from_settings()
    while True:
        try:
            return await call()
        except LLMError as e:
            delay = budget.delay_for(e)
            if delay is None:
                raise
            await _wait(e, delay, budget, chat_id)
