"""The retention job: select expired chats, archive each, then delete it.

Per chat (docs/design/chat-retention.md §3):

1. claim it: `pg_try_advisory_xact_lock` in a transaction on the run's
   claim connection, held until the chat is done (another runner skips it);
2. snapshot it in a short read;
3. upload the archive (no transaction open);
4. delete it in one short transaction, after `FOR UPDATE SKIP LOCKED` on
   the chat row and a re-check that nothing blocks it and that it is still
   exactly what was archived.

Upload failed → nothing deleted. Delete failed after the upload → the next
run uploads to the same key again, then deletes. Never logs or records chat
content or titles: chat ids and counts only.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    and_,
    case,
    exists,
    func,
    literal,
    or_,
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models.agent import Agent
from app.models.chat import Chat, Message
from app.models.chat_wakeup import TIMER, WATCH, ChatWakeup
from app.models.mcp_task import McpTask
from app.models.pending_approval import PendingToolApproval
from app.models.pending_completion import PendingCompletion
from app.models.retention_run import (
    COMPLETED,
    FAILED,
    INTERRUPTED,
    PARTIAL,
    QUEUED,
    REFUSED,
    RUNNING,
    ChatRetentionRun,
)
from app.models.tool_call_result import ToolCallResult
from app.models.user import User
from app.services.chat_deletion import delete_chats_and_rows, remove_stored_files
from app.services.retention import archive as fmt
from app.services.retention.policy import (
    RetentionPolicy,
    archive_prefix,
    describe_destination,
    destination_configured,
    next_fire,
    run_is_due,
    schedule_error,
)

logger = logging.getLogger(__name__)

NO_AGENT = "(no agent)"

# Why a chat was left alone this run. The first four are decided in SQL
# (selection), the rest per chat.
PARKED = "parked"
PENDING_APPROVAL = "pending_approval"
WAKEUP = "wakeup"
MCP_TASK = "mcp_task"
RUNNING_TURN = "running"
BACKGROUND_JOB = "background_job"
CLAIMED = "claimed"  # another runner has it
LOCKED = "locked"  # its row was locked at delete time
CHANGED = "changed"  # written to after the snapshot: archived again next run
GONE = "gone"  # deleted meanwhile
# No longer qualifies, by the database at that moment: it was written to,
# its agent's retention changed, or retention was switched off. Checked when
# it is snapshotted and again, under the row lock, right before the delete.
NOT_ELIGIBLE = "not_eligible"
TOO_LARGE = "too_large"  # its archive would exceed what a restore reads back

_UNSET: Any = object()


def _key(chat_id: Any) -> int:
    """The advisory lock key of a chat: 64 bits of a namespaced hash (a
    collision only makes one runner skip a chat the other isn't doing)."""
    digest = hashlib.sha256(f"sinas.chat-retention:{chat_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _agent_label(namespace: Optional[str], name: Optional[str]) -> str:
    return f"{namespace}/{name}" if namespace and name else NO_AGENT


def _safe_error(error: BaseException) -> str:
    """An error for a log line or the run record: the kind, never a SQL
    statement's parameters or a request body (which could hold content)."""
    from app.services.retention.store import ArchiveUploadError

    if isinstance(error, ArchiveUploadError):
        return str(error)
    orig = getattr(error, "orig", None)
    if orig is not None:
        return f"{type(error).__name__} ({type(orig).__name__})"
    return type(error).__name__


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _days_expr(global_days: int):
    return case((Agent.retention_days.isnot(None), Agent.retention_days), else_=literal(global_days))


def _source_expr():
    return case((Agent.retention_days.isnot(None), literal("agent")), else_=literal("global"))


def blocked_expr(now: datetime):
    """Why a chat must be left alone, from what Postgres knows: NULL when
    nothing blocks it. Correlated on Chat.id."""
    now_lit = literal(now, DateTime(timezone=True))
    return case(
        (exists().where(PendingCompletion.chat_id == Chat.id), literal(PARKED)),
        (
            exists().where(
                PendingToolApproval.chat_id == Chat.id,
                PendingToolApproval.approved.is_(None),
                or_(PendingToolApproval.expires_at.is_(None), PendingToolApproval.expires_at > now_lit),
            ),
            literal(PENDING_APPROVAL),
        ),
        (
            exists().where(
                ChatWakeup.chat_id == Chat.id,
                ChatWakeup.cancelled.is_(False),
                or_(
                    and_(ChatWakeup.kind == TIMER, ChatWakeup.delivered_at.is_(None)),
                    and_(
                        ChatWakeup.kind == WATCH,
                        or_(ChatWakeup.expires_at.is_(None), ChatWakeup.expires_at > now_lit),
                    ),
                ),
            ),
            literal(WAKEUP),
        ),
        (
            exists().where(
                McpTask.chat_id == Chat.id,
                or_(McpTask.finished_at.is_(None), McpTask.next_poll_at.isnot(None)),
            ),
            literal(MCP_TASK),
        ),
        else_=literal(None),
    )


def eligible_stmt(global_days: int, min_days: int, now: datetime):
    """Chats idle past their retention period, oldest first, with their
    agent, effective days, where the days came from, and a blocked reason.

    Idle = no chat update and no message within the period: messages don't
    touch chats.updated_at, so both are checked."""
    days = _days_expr(global_days)
    now_lit = literal(now, DateTime(timezone=True))
    cutoff = now_lit - func.make_interval(0, 0, 0, days)
    return (
        select(
            Chat.id,
            Chat.updated_at,
            Chat.agent_namespace,
            Chat.agent_name,
            days.label("days"),
            _source_expr().label("source"),
            blocked_expr(now).label("blocked"),
        )
        .select_from(Chat)
        .outerjoin(Agent, Agent.id == Chat.agent_id)
        .where(
            # Served by ix_chats_updated_at_id: only chats older than the
            # shortest policy are looked at.
            Chat.updated_at < literal(now - timedelta(days=min_days), DateTime(timezone=True)),
            days > 0,
            Chat.updated_at < cutoff,
            ~exists().where(Message.chat_id == Chat.id, Message.created_at >= cutoff),
        )
    )


async def agent_overrides(db: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(Agent.namespace, Agent.name, Agent.retention_days)
            .where(Agent.retention_days.isnot(None))
            .order_by(Agent.namespace, Agent.name)
        )
    ).all()
    return [{"agent": f"{ns}/{name}", "retention_days": days} for ns, name, days in rows]


def _shortest_days(policy: RetentionPolicy, overrides: list[dict[str, Any]]) -> Optional[int]:
    """The shortest positive retention anywhere; None = nothing can expire."""
    days = [o["retention_days"] for o in overrides if o["retention_days"] and o["retention_days"] > 0]
    if policy.global_days > 0:
        days.append(policy.global_days)
    return min(days) if days else None


def refusal(policy: RetentionPolicy, has_store: bool) -> Optional[str]:
    """Why a real run can't go ahead now; None when it can."""
    if not policy.enabled:
        return "Chat retention is disabled (CHAT_RETENTION_ENABLED is not true)."
    if policy.action == "archive" and not has_store:
        return (
            "CHAT_RETENTION_ACTION is 'archive' but no archive destination is configured "
            "(CHAT_ARCHIVE_S3_BUCKET). Nothing is deleted without an archive unless "
            "CHAT_RETENTION_ACTION=delete."
        )
    return None


async def ineligible_reason(db: AsyncSession, chat_id: uuid.UUID, now: datetime) -> Optional[str]:
    """Why the chat doesn't qualify for retention right now, by what the
    database says in this transaction under the CURRENT policy (switch,
    default, its agent's retentionDays): None when it does."""
    policy = RetentionPolicy.from_settings()
    if not policy.enabled:
        return NOT_ELIGIBLE
    row = (
        await db.execute(eligible_stmt(policy.global_days, 0, now).where(Chat.id == chat_id))
    ).one_or_none()
    if row is None:
        return NOT_ELIGIBLE
    return row.blocked or None


async def eligible_snapshot(sessions, chat_id: uuid.UUID, now: datetime) -> tuple[Optional["Snapshot"], Optional[str]]:
    """The chat's snapshot and its eligibility from one consistent read
    (REPEATABLE READ: both see the same database state). (snapshot, None)
    when it qualifies, else (None, reason)."""
    async with sessions() as db:
        await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        reason = await ineligible_reason(db, chat_id, now)
        if reason:
            return None, reason
        snapshot = await load_snapshot(db, chat_id)
        return (snapshot, None) if snapshot is not None else (None, GONE)


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    chat: dict[str, Any]
    created_at: datetime
    messages: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    workbench: dict[str, Any]

    @property
    def digest(self) -> str:
        return fmt.content_digest(self.chat, self.messages, self.tool_results)


async def load_snapshot(db: AsyncSession, chat_id: uuid.UUID) -> Optional[Snapshot]:
    """Everything of a chat that goes into its archive. None if it's gone."""
    from app.models.file import Collection, File, FileVersion

    chat = (
        await db.execute(select(Chat).where(Chat.id == chat_id).execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if chat is None:
        return None
    user_email = (await db.execute(select(User.email).where(User.id == chat.user_id))).scalar_one_or_none()
    messages = (
        await db.execute(
            select(Message).where(Message.chat_id == chat_id).order_by(Message.created_at, Message.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    tool_results = (
        await db.execute(
            select(ToolCallResult)
            .where(ToolCallResult.chat_id == chat_id)
            .order_by(ToolCallResult.created_at, ToolCallResult.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    files = (
        await db.execute(
            select(File.name, File.content_type, FileVersion.version_number, FileVersion.size_bytes,
                   FileVersion.hash_sha256)
            .join(Collection, File.collection_id == Collection.id)
            .join(FileVersion, FileVersion.file_id == File.id)
            .where(
                Collection.kind == "workbench",
                Collection.namespace == "_chat",
                Collection.name == str(chat_id),
            )
            .order_by(File.name, FileVersion.version_number)
        )
    ).all()
    workbench: dict[str, dict[str, Any]] = {}
    for name, content_type, version, size, sha in files:
        entry = workbench.setdefault(name, {"path": name, "content_type": content_type, "versions": []})
        entry["versions"].append({"version": version, "size_bytes": size, "sha256": sha})

    return Snapshot(
        chat=fmt.jsonable({
            "id": chat.id,
            "user_id": chat.user_id,
            "user_email": user_email,
            "agent_id": chat.agent_id,
            "agent_namespace": chat.agent_namespace,
            "agent_name": chat.agent_name,
            "title": chat.title,
            "archived": bool(chat.archived),
            "created_at": chat.created_at,
            "updated_at": chat.updated_at,
            "expires_at": chat.expires_at,
            "session_key": chat.session_key,
            "job_timeout": chat.job_timeout,
            "keep_alive": chat.keep_alive,
            "chat_metadata": chat.chat_metadata,
        }),
        created_at=chat.created_at,
        messages=[
            fmt.jsonable({
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "tool_calls": m.tool_calls,
                "tool_call_id": m.tool_call_id,
                "name": m.name,
                "created_at": m.created_at,
            })
            for m in messages
        ],
        tool_results=[
            fmt.jsonable({
                "id": r.id,
                "tool_call_id": r.tool_call_id,
                "user_id": r.user_id,
                "tool_name": r.tool_name,
                "arguments": r.arguments,
                "result": r.result,
                "result_size": r.result_size,
                "status_code": r.status_code,
                "duration_ms": r.duration_ms,
                "source": r.source,
                "created_at": r.created_at,
                "expires_at": r.expires_at,
            })
            for r in tool_results
        ],
        workbench={"files": list(workbench.values())},
    )


def _last_activity(snapshot: Snapshot) -> Optional[str]:
    times = [snapshot.chat.get("updated_at")] + [m["created_at"] for m in snapshot.messages]
    times = [t for t in times if t]
    return max(times) if times else None


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def delete_chat(db: AsyncSession, chat_id: uuid.UUID, digest: str, now: datetime) -> tuple[str, int, list[str]]:
    """Delete an archived chat, if it still qualifies (by the database now,
    under the current policy) and is still exactly what was archived.
    Returns ("deleted", messages, stored paths to remove after the commit)
    or (skip reason, 0, []). Commits on delete, rolls back otherwise.

    Everything is checked under the row lock: a message insert's foreign key
    check waits for it, so nothing can land between the checks and the
    delete."""
    from app.services import chat_steering

    locked = (
        await db.execute(select(Chat.id).where(Chat.id == chat_id).with_for_update(skip_locked=True))
    ).scalar_one_or_none()
    if locked is None:
        gone = (await db.execute(select(Chat.id).where(Chat.id == chat_id))).scalar_one_or_none() is None
        await db.rollback()
        return (GONE if gone else LOCKED), 0, []
    reason = await ineligible_reason(db, chat_id, now)
    if reason:
        await db.rollback()
        return reason, 0, []
    if await chat_steering.is_chat_locked(str(chat_id)):
        await db.rollback()
        return RUNNING_TURN, 0, []
    current = await load_snapshot(db, chat_id)
    if current is None or current.digest != digest:
        await db.rollback()
        return CHANGED, 0, []

    # Children first, explicitly: the database's ON DELETE rules for these
    # differ from what the models declare (see the design doc's table).
    deleted_messages, paths = await delete_chats_and_rows(db, [chat_id])
    await db.commit()
    return "deleted", deleted_messages, paths


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


class Claims:
    """Per-chat claims for one runner: a transaction-level advisory lock in
    a transaction on this runner's own connection, held until `release`.
    Transaction-level because session-level advisory locks don't survive
    pgbouncer's transaction pooling; an open transaction pins its server
    connection. Released if the runner dies (the connection goes)."""

    def __init__(self, engine: AsyncEngine):
        self.engine = engine
        self.conn = None
        self.trans = None

    async def __aenter__(self) -> "Claims":
        self.conn = await self.engine.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.release()
        await self.conn.close()

    async def try_claim(self, chat_id: Any) -> bool:
        await self.release()
        self.trans = await self.conn.begin()
        got = (
            await self.conn.execute(select(func.pg_try_advisory_xact_lock(literal(_key(chat_id), BigInteger))))
        ).scalar()
        if not got:
            await self.release()
        return bool(got)

    async def release(self) -> None:
        if self.trans is not None:
            trans, self.trans = self.trans, None
            try:
                await trans.rollback()
            except Exception:  # a dead connection releases its locks anyway
                logger.warning("Chat retention: could not release a claim cleanly", exc_info=False)


async def _has_live_background_job(chat_id: Any) -> bool:
    from app.services.chat_wakeups import list_background_jobs

    jobs = await list_background_jobs(str(chat_id))
    return any(job.get("status") in ("queued", "running") for job in jobs)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@dataclass
class _Tally:
    candidates: int = 0
    archived: int = 0
    deleted: int = 0
    messages_deleted: int = 0
    bytes_archived: int = 0
    skipped: Counter = field(default_factory=Counter)
    failed: Counter = field(default_factory=Counter)
    per_agent: dict[str, dict[str, int]] = field(default_factory=dict)
    more_remaining: bool = False
    cursor: Optional[tuple[datetime, uuid.UUID]] = None  # the last chat looked at

    def agent(self, label: str) -> dict[str, int]:
        return self.per_agent.setdefault(label, {"archived": 0, "deleted": 0, "messages_deleted": 0})

    def counts(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "archived": self.archived,
            "deleted": self.deleted,
            "messages_deleted": self.messages_deleted,
            "bytes_archived": self.bytes_archived,
            "skipped": dict(self.skipped),
            "failed": dict(self.failed),
            "more_remaining": self.more_remaining,
        }


def policy_snapshot(policy: RetentionPolicy, overrides: list[dict[str, Any]]) -> dict[str, Any]:
    return {**policy.describe(), "destination": describe_destination(), "agent_overrides": overrides}


async def _start_run(sessions, run_id, trigger, actor_user_id, actor_email, policy_info) -> uuid.UUID:
    async with sessions() as db:
        run = await db.get(ChatRetentionRun, run_id) if run_id else None
        if run is None:
            run = ChatRetentionRun(
                id=run_id or uuid.uuid4(),
                trigger=trigger,
                actor_user_id=uuid.UUID(str(actor_user_id)) if actor_user_id else None,
                actor_email=actor_email,
            )
            db.add(run)
        run.status = RUNNING
        run.started_at = datetime.now(UTC)
        run.policy = policy_info
        await db.commit()
        return run.id


def _record(run: ChatRetentionRun, tally: Optional[_Tally]) -> None:
    run.counts = tally.counts() if tally else None
    run.per_agent = dict(tally.per_agent) if tally else None
    if tally and tally.cursor:
        run.cursor_updated_at, run.cursor_chat_id = tally.cursor


async def _save_progress(sessions, run_id, tally: _Tally) -> None:
    """Counts and cursor so far (after every batch): an interrupted run's
    record says what it did, and a continuation knows where to start."""
    async with sessions() as db:
        run = await db.get(ChatRetentionRun, run_id)
        if run is not None:
            _record(run, tally)
            await db.commit()


async def _finish_run(sessions, run_id, status, tally: Optional[_Tally], error: Optional[str]) -> dict[str, Any]:
    async with sessions() as db:
        run = await db.get(ChatRetentionRun, run_id)
        run.status = status
        run.finished_at = datetime.now(UTC)
        _record(run, tally)
        run.error = error
        await db.commit()
        return serialize_run(run)


async def _continue_from(sessions, run_id) -> Optional[tuple[datetime, uuid.UUID]]:
    """Where this run starts: after the cursor of the previous run when that
    one stopped before the end (partial, interrupted, failed); from the
    oldest chat when it completed. So a chain of capped runs walks forward
    past chats that keep failing, and a fresh chain retries them later."""
    async with sessions() as db:
        previous = (
            await db.execute(
                select(ChatRetentionRun)
                .where(
                    ChatRetentionRun.id != run_id,
                    ChatRetentionRun.status.in_((COMPLETED, PARTIAL, INTERRUPTED, FAILED)),
                )
                .order_by(ChatRetentionRun.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if previous is None or previous.status == COMPLETED or previous.cursor_chat_id is None:
        return None
    return previous.cursor_updated_at, previous.cursor_chat_id


def serialize_run(run: Optional[ChatRetentionRun]) -> Optional[dict[str, Any]]:
    if run is None:
        return None
    return fmt.jsonable({
        "id": run.id,
        "trigger": run.trigger,
        "status": run.status,
        "actor_email": run.actor_email,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "counts": run.counts,
        "per_agent": run.per_agent,
        "error": run.error,
    })


async def run_retention(
    *,
    trigger: str,
    actor_user_id: Optional[str] = None,
    actor_email: Optional[str] = None,
    run_id: Optional[uuid.UUID] = None,
    store: Any = _UNSET,
    engine: Optional[AsyncEngine] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """One real run. Records it in chat_retention_runs and returns the
    record. `store`/`engine`/`now` are for tests."""
    from app.core.database import async_engine
    from app.services.retention.store import get_archive_store

    engine = engine or async_engine
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    policy = RetentionPolicy.from_settings()
    now = now or datetime.now(UTC)
    if store is _UNSET:
        store = get_archive_store()

    async with sessions() as db:
        overrides = await agent_overrides(db)
    run_id = await _start_run(
        sessions, run_id, trigger, actor_user_id, actor_email, policy_snapshot(policy, overrides)
    )
    refused = refusal(policy, store is not None)
    if refused:
        logger.warning("Chat retention run %s refused: %s", run_id, refused)
        return await _finish_run(sessions, run_id, REFUSED, None, refused)

    tally = _Tally()
    start_after = await _continue_from(sessions, run_id)
    try:
        await _process(engine, sessions, policy, overrides, store, now, tally, run_id, start_after)
    except Exception as e:
        error = _safe_error(e)
        logger.error("Chat retention run %s failed: %s", run_id, error)
        return await _finish_run(sessions, run_id, FAILED, tally, error)
    except BaseException:
        # Cancelled (job timeout, worker shutdown): record what it did, then
        # let the cancellation through. Shielded so the record is written
        # even though this task is being cancelled.
        logger.warning(
            "Chat retention run %s interrupted after %d archived, %d deleted", run_id, tally.archived, tally.deleted
        )
        try:
            await asyncio.shield(
                _finish_run(sessions, run_id, INTERRUPTED, tally, "Interrupted: the worker stopped or the job timed out")
            )
        except BaseException:  # pragma: no cover - best effort while going down
            logger.error("Chat retention run %s: could not record the interruption", run_id)
        raise

    status = PARTIAL if tally.more_remaining else COMPLETED
    logger.info(
        "Chat retention run %s (%s) %s: %d candidates, %d archived, %d deleted (%d messages), "
        "skipped %s, failed %s",
        run_id, trigger, status, tally.candidates, tally.archived, tally.deleted,
        tally.messages_deleted, dict(tally.skipped), dict(tally.failed),
    )
    return await _finish_run(sessions, run_id, status, tally, None)


async def _process(engine, sessions, policy, overrides, store, now, tally: _Tally, run_id, start_after=None) -> None:
    min_days = _shortest_days(policy, overrides)
    if min_days is None:
        return
    started = time.monotonic()
    processed = 0
    cursor: Optional[tuple[datetime, uuid.UUID]] = start_after

    def out_of_budget() -> bool:
        if policy.max_chats_per_run and processed >= policy.max_chats_per_run:
            return True
        return bool(policy.max_run_seconds) and time.monotonic() - started >= policy.max_run_seconds

    async with Claims(engine) as claims:
        while True:
            stmt = eligible_stmt(policy.global_days, min_days, now)
            if cursor is not None:
                stmt = stmt.where(tuple_(Chat.updated_at, Chat.id) > tuple_(
                    literal(cursor[0], DateTime(timezone=True)), literal(cursor[1], Chat.id.type)
                ))
            stmt = stmt.order_by(Chat.updated_at, Chat.id).limit(policy.batch_size)
            async with sessions() as db:
                rows = (await db.execute(stmt)).all()
            if not rows:
                return
            for row in rows:
                # Budget first, and skipped chats count against it too: a
                # backlog of blocked chats must not keep a run scanning past
                # the operator's chat cap or time limit.
                if out_of_budget():
                    tally.more_remaining = True
                    return
                if row.blocked:
                    tally.candidates += 1
                    tally.skipped[row.blocked] += 1
                    cursor = tally.cursor = (row.updated_at, row.id)
                    processed += 1
                    continue
                tally.candidates += 1
                cursor = tally.cursor = (row.updated_at, row.id)
                processed += 1
                await _process_chat(sessions, claims, store, policy, row, now, tally)
            await _save_progress(sessions, run_id, tally)
            if len(rows) < policy.batch_size:
                return


async def _process_chat(sessions, claims: Claims, store, policy, row, now, tally: _Tally) -> None:
    from app.services import chat_steering
    from app.services.retention.store import ArchiveUploadError

    chat_id = row.id
    label = _agent_label(row.agent_namespace, row.agent_name)
    if not await claims.try_claim(chat_id):
        tally.skipped[CLAIMED] += 1
        return
    stage = "check"
    try:
        if await chat_steering.is_chat_locked(str(chat_id)):
            tally.skipped[RUNNING_TURN] += 1
            return
        if await _has_live_background_job(chat_id):
            tally.skipped[BACKGROUND_JOB] += 1
            return
        stage = "snapshot"
        snapshot, reason = await eligible_snapshot(sessions, chat_id, now)
        if snapshot is None:
            tally.skipped[reason] += 1
            return

        if store is not None:
            stage = "upload"
            built = fmt.build_archive(
                {
                    "sinas_version": _sinas_version(),
                    "archived_at": datetime.now(UTC).isoformat(),
                    "retention": {
                        "days": row.days,
                        "source": row.source,
                        "action": policy.action,
                        "last_activity_at": _last_activity(snapshot),
                    },
                },
                snapshot.chat,
                snapshot.messages,
                snapshot.tool_results,
                snapshot.workbench,
            )
            if built.uncompressed_bytes > fmt.MAX_UNCOMPRESSED_BYTES:
                # A restore couldn't read it back: keep the chat, say why.
                tally.skipped[TOO_LARGE] += 1
                logger.warning(
                    "Chat retention: chat %s kept, too large to archive safely (%d bytes uncompressed)",
                    chat_id, built.uncompressed_bytes,
                )
                return
            key = fmt.object_key(archive_prefix(), chat_id, snapshot.created_at)
            await store.put(
                key,
                built.body,
                content_type=fmt.CONTENT_TYPE,
                metadata={
                    "sinas-chat-id": str(chat_id),
                    "sinas-format-version": str(fmt.FORMAT_VERSION),
                    "sinas-sha256": built.sha256,
                },
            )
            tally.archived += 1
            tally.bytes_archived += len(built.body)
            tally.agent(label)["archived"] += 1

        stage = "delete"
        async with sessions() as db:
            outcome, messages, paths = await delete_chat(db, chat_id, snapshot.digest, now)
        if outcome != "deleted":
            tally.skipped[outcome] += 1
            return
        tally.deleted += 1
        tally.messages_deleted += messages
        stats = tally.agent(label)
        stats["deleted"] += 1
        stats["messages_deleted"] += messages
        if paths:
            stage = "workbench_files"
            try:
                await remove_stored_files(paths)
            except Exception as e:  # the chat is deleted; a stale blob is a leak, not a failure
                logger.warning("Chat retention: workbench files of chat %s not removed: %s", chat_id, _safe_error(e))
    except ArchiveUploadError as e:
        tally.failed["upload"] += 1
        logger.warning("Chat retention: archiving chat %s failed, kept: %s", chat_id, e)
    except Exception as e:
        tally.failed[stage] += 1
        logger.warning("Chat retention: chat %s failed at %s, kept: %s", chat_id, stage, _safe_error(e))
    finally:
        await claims.release()


def _sinas_version() -> str:
    from app._version import __version__

    return __version__


# ---------------------------------------------------------------------------
# Scheduled tick, dry run, status
# ---------------------------------------------------------------------------


async def _latest_runs(db: AsyncSession) -> tuple[Optional[ChatRetentionRun], Optional[ChatRetentionRun]]:
    latest = (
        await db.execute(select(ChatRetentionRun).order_by(ChatRetentionRun.created_at.desc()).limit(1))
    ).scalar_one_or_none()
    scheduled = (
        await db.execute(
            select(ChatRetentionRun)
            .where(ChatRetentionRun.trigger == "schedule")
            .order_by(ChatRetentionRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return latest, scheduled


def _in_progress(run: Optional[ChatRetentionRun], policy: RetentionPolicy, now: datetime) -> bool:
    """A run queued or running, and recent enough to still be alive."""
    if run is None or run.status not in (QUEUED, RUNNING):
        return False
    if run.status == QUEUED:
        # A worker picks a queued run up within seconds; one still queued
        # after this never will (its job failed or was lost).
        return run.created_at is not None and now - run.created_at < QUEUED_STALE_AFTER
    since = run.started_at or run.created_at
    limit = timedelta(seconds=(policy.max_run_seconds or 6 * 3600) + 15 * 60)
    return since is not None and now - since < limit


QUEUED_STALE_AFTER = timedelta(minutes=15)
STALE_ERROR = "Interrupted: no longer running (the worker stopped or the job was lost)"


def _stale(run: Optional[ChatRetentionRun], policy: RetentionPolicy, now: datetime) -> bool:
    """Queued or running on record, but past the window a live one stays in."""
    return run is not None and run.status in (QUEUED, RUNNING) and not _in_progress(run, policy, now)


async def _mark_stale(db: AsyncSession, policy: RetentionPolicy, now: datetime) -> None:
    """Record runs that died without saying so (process killed) as
    interrupted, keeping the counts and cursor they saved per batch."""
    rows = (
        await db.execute(select(ChatRetentionRun).where(ChatRetentionRun.status.in_((QUEUED, RUNNING))))
    ).scalars().all()
    changed = False
    for run in rows:
        if _stale(run, policy, now):
            run.status, run.finished_at, run.error = INTERRUPTED, now, STALE_ERROR
            changed = True
    if changed:
        await db.commit()


async def _last_outcome(db: AsyncSession) -> Optional[str]:
    """How the most recent run that got going ended, whatever started it."""
    return (
        await db.execute(
            select(ChatRetentionRun.status)
            .where(ChatRetentionRun.status.in_((COMPLETED, PARTIAL, INTERRUPTED, FAILED)))
            .order_by(ChatRetentionRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def scheduled_tick(engine: Optional[AsyncEngine] = None, now: Optional[datetime] = None) -> Optional[dict]:
    """The cron tick: start a run when the schedule says so. None = not due."""
    from app.core.database import async_engine

    policy = RetentionPolicy.from_settings()
    if not policy.enabled:
        return None
    problem = schedule_error(policy.schedule)
    if problem:
        logger.warning("Chat retention not scheduled: %s", problem)
        return None
    now = now or datetime.now(UTC)
    sessions = async_sessionmaker(engine or async_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as db:
        latest, scheduled = await _latest_runs(db)
        if _in_progress(latest, policy, now):
            return None
        await _mark_stale(db, policy, now)
        # A partial or interrupted run continues whoever started it (the
        # schedule, Run now or the CLI).
        last_outcome = await _last_outcome(db)
    last_start = (scheduled.started_at or scheduled.created_at) if scheduled else None
    if not run_is_due(policy.schedule, now, last_start, last_outcome):
        return None
    return await run_retention(trigger="schedule", engine=engine)


async def preview(db: AsyncSession, now: Optional[datetime] = None) -> dict[str, Any]:
    """The dry run: what a run would archive and delete now, per agent, and
    what it would leave alone and why. Reads only. Turns running right now
    and background jobs (Redis) are checked when the run gets to a chat,
    not here."""
    policy = RetentionPolicy.from_settings()
    now = now or datetime.now(UTC)
    overrides = await agent_overrides(db)
    min_days = _shortest_days(policy, overrides)
    agents: dict[tuple, dict[str, Any]] = {}
    totals = {"chats": 0, "messages": 0, "blocked": Counter()}
    if min_days is not None:
        sub = eligible_stmt(policy.global_days, min_days, now).subquery()
        rows = (
            await db.execute(
                select(sub.c.agent_namespace, sub.c.agent_name, sub.c.days, sub.c.source, sub.c.blocked,
                       func.count())
                .group_by(sub.c.agent_namespace, sub.c.agent_name, sub.c.days, sub.c.source, sub.c.blocked)
            )
        ).all()
        message_rows = (
            await db.execute(
                select(sub.c.agent_namespace, sub.c.agent_name, sub.c.days, sub.c.source, func.count(Message.id))
                .join(Message, Message.chat_id == sub.c.id)
                .where(sub.c.blocked.is_(None))
                .group_by(sub.c.agent_namespace, sub.c.agent_name, sub.c.days, sub.c.source)
            )
        ).all()
        for ns, name, days, source, blocked, count in rows:
            entry = agents.setdefault(
                (_agent_label(ns, name), days, source),
                {"agent": _agent_label(ns, name), "retention_days": days, "source": source,
                 "chats": 0, "messages": 0, "blocked": {}},
            )
            if blocked:
                entry["blocked"][blocked] = entry["blocked"].get(blocked, 0) + count
                totals["blocked"][blocked] += count
            else:
                entry["chats"] += count
                totals["chats"] += count
        for ns, name, days, source, count in message_rows:
            entry = agents.get((_agent_label(ns, name), days, source))
            if entry is not None:
                entry["messages"] += count
                totals["messages"] += count
    from app.services.retention.store import get_archive_store

    return {
        "dry_run": True,
        "as_of": now.isoformat(),
        "policy": policy.describe(),
        "destination": describe_destination(),
        "would_archive": destination_configured(),
        "refusal": refusal(policy, get_archive_store() is not None),
        "agents": sorted(agents.values(), key=lambda e: (e["agent"], e["retention_days"])),
        "totals": {"chats": totals["chats"], "messages": totals["messages"], "blocked": dict(totals["blocked"])},
        "notes": [
            "Chats with a turn running or a background job queued at the time the run reaches "
            "them are skipped then; the dry run doesn't check those.",
        ],
    }


async def status(db: AsyncSession, now: Optional[datetime] = None) -> dict[str, Any]:
    from app.services.retention.store import get_archive_store

    policy = RetentionPolicy.from_settings()
    now = now or datetime.now(UTC)
    latest, _ = await _latest_runs(db)
    last_finished = (
        await db.execute(
            select(ChatRetentionRun)
            .where(ChatRetentionRun.finished_at.isnot(None))
            .order_by(ChatRetentionRun.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    problem = schedule_error(policy.schedule)
    next_run = None
    if policy.enabled and not problem:
        next_run = next_fire(policy.schedule, now).isoformat()
    return {
        "policy": policy.describe(),
        "destination": describe_destination(),
        "agent_overrides": await agent_overrides(db),
        "schedule_error": problem,
        "next_run": next_run,
        "refusal": refusal(policy, get_archive_store() is not None),
        "in_progress": serialize_run(latest) if _in_progress(latest, policy, now) else None,
        # One that died without recording it shows as what it is.
        "last_run": (
            {**serialize_run(latest), "status": INTERRUPTED, "error": STALE_ERROR}
            if _stale(latest, policy, now)
            else serialize_run(last_finished)
        ),
    }

