"""Depth-aware admission control for the shared (trusted) worker pool.

Prevents the nested-execution deadlock: a parent `shared_pool` function blocked
while synchronously waiting on a child can starve the pool when every worker is
held by a parent. We reserve `settings.shared_pool_reserve` slots that only
NESTED executions (depth > 0) may use, so a child can always find capacity.

Opt-in: `shared_pool_reserve = 0` disables it entirely (default — no behaviour
change). For full single-chain safety set the reserve to
`max_execution_depth - 1` AND keep `default_worker_count >= max_execution_depth`
(a chain of depth D holds D slots at once). A smaller reserve still breaks the
common contention case (several shallow chains) while throttling top-level work
less.

The in-flight set is a Redis sorted-set keyed by execution_id with a timestamp
score, so the count is correct across the multiple queue-worker processes and
self-heals if a process dies mid-execution (stale entries age out).
"""

from __future__ import annotations

import contextlib
import logging
import time

from app.core.config import settings
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

_INFLIGHT_ZSET = "sinas:shared:toplevel_inflight"
# Entries older than this are treated as dead (crashed worker) and pruned, so a
# leak can't permanently throttle the pool. Comfortably above any real run.
_ENTRY_TTL_SECONDS = 3600


class SharedPoolSaturated(Exception):
    """Raised when top-level shared-pool admission is at capacity."""


@contextlib.asynccontextmanager
async def shared_pool_admission(depth: int, execution_id: str):
    """Admit a shared-pool execution, reserving slots for nested calls.

    Nested executions (depth > 0) bypass the gate — that is the whole point of
    the reserve. Top-level (depth 0) executions are capped at
    `default_worker_count - shared_pool_reserve`. Raises `SharedPoolSaturated`
    (fail-fast, retryable) when the top-level cap is reached.
    """
    reserve = settings.shared_pool_reserve
    if reserve <= 0 or depth > 0:
        # Disabled, or a nested call that must always be allowed through.
        yield
        return

    cap = max(1, settings.default_worker_count - reserve)
    redis = await get_redis()
    now = time.time()

    # Prune dead entries, then count live top-level executions.
    await redis.zremrangebyscore(_INFLIGHT_ZSET, 0, now - _ENTRY_TTL_SECONDS)
    live = await redis.zcard(_INFLIGHT_ZSET)
    if live >= cap:
        raise SharedPoolSaturated(
            f"Shared worker pool saturated: {live}/{cap} top-level slots in use "
            f"({reserve} reserved for nested calls). Retry shortly or scale workers."
        )

    await redis.zadd(_INFLIGHT_ZSET, {execution_id: now})
    try:
        yield
    finally:
        await redis.zrem(_INFLIGHT_ZSET, execution_id)
