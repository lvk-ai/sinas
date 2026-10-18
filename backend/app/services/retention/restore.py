"""Restore an archived chat into Postgres (`python -m app.cli restore-chat`).

The archive is read with credentials the operator provides (a local file,
or s3:// with the standard AWS chain / --profile), verified in full
(`archive.parse_archive`), then planned: what would be written and what
would conflict. A dry run stops there. A restore writes the chat and its
messages under their original ids in one transaction, or nothing.

The restored chat's updated_at is now: it gets a full retention period
again, rather than being archived and deleted by the next run.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.chat import Chat, Message
from app.models.tool_call_result import ToolCallResult
from app.models.user import User
from app.services.retention.archive import ParsedArchive


def _dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class RestorePlan:
    chat_id: str
    title: str
    agent: Optional[str]
    owner_user_id: Optional[uuid.UUID] = None
    owner: Optional[str] = None  # how the owner was found, for the report
    agent_id: Optional[uuid.UUID] = None
    messages: int = 0
    tool_results: int = 0  # restorable (not expired)
    tool_results_expired: int = 0
    conflicts: list[str] = field(default_factory=list)  # any → nothing is written
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.conflicts

    def report(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "agent": self.agent,
            "owner": self.owner,
            "messages": self.messages,
            "tool_results": self.tool_results,
            "tool_results_expired": self.tool_results_expired,
            "conflicts": self.conflicts,
            "notes": self.notes,
        }


async def plan_restore(
    db: AsyncSession, archive: ParsedArchive, *, user_email: Optional[str] = None, now: Optional[datetime] = None
) -> RestorePlan:
    now = now or datetime.now(UTC)
    chat = archive.chat
    ns, name = chat.get("agent_namespace"), chat.get("agent_name")
    plan = RestorePlan(
        chat_id=chat["id"],
        title=chat.get("title") or "",
        agent=f"{ns}/{name}" if ns and name else None,
        messages=len(archive.messages),
    )

    chat_id = uuid.UUID(chat["id"])
    if (await db.execute(select(Chat.id).where(Chat.id == chat_id))).scalar_one_or_none():
        plan.conflicts.append(f"chat {chat_id} already exists")
    message_ids = [uuid.UUID(m["id"]) for m in archive.messages]
    if message_ids:
        taken = (await db.execute(select(Message.id).where(Message.id.in_(message_ids)))).scalars().all()
        if taken:
            plan.conflicts.append(f"{len(taken)} message id(s) already exist (e.g. {taken[0]})")

    # Owner: the archived user, else the archived email, else --user-email.
    owner = await db.get(User, uuid.UUID(chat["user_id"]))
    if owner is not None:
        plan.owner = f"archived user {owner.id}"
    else:
        for email, how in ((chat.get("user_email"), "archived email"), (user_email, "--user-email")):
            if not email:
                continue
            owner = (
                await db.execute(select(User).where(func.lower(User.email) == email.strip().lower()))
            ).scalars().first()
            if owner is not None:
                plan.owner = f"{how} ({owner.email})"
                break
    if owner is None:
        plan.conflicts.append(
            "the archived owner doesn't exist here (by id or email); pass --user-email to restore it to another user"
        )
    else:
        plan.owner_user_id = owner.id

    if ns and name:
        agent = (
            await db.execute(select(Agent).where(Agent.namespace == ns, Agent.name == name))
        ).scalar_one_or_none()
        if agent is not None:
            plan.agent_id = agent.id
        else:
            plan.notes.append(f"agent {ns}/{name} doesn't exist here: the chat is restored unlinked")

    for r in archive.tool_results:
        expires = _dt(r.get("expires_at"))
        if expires is not None and expires > now:
            plan.tool_results += 1
        else:
            plan.tool_results_expired += 1
    if plan.tool_results_expired:
        plan.notes.append(
            f"{plan.tool_results_expired} stored tool result(s) are past their expiry and are not restored "
            "(the messages hold the results as the agent saw them)"
        )
    files = (archive.header.get("workbench") or {}).get("files") or []
    if files:
        plan.notes.append(f"{len(files)} workbench file(s) were not archived and are not restored")
    return plan


async def apply_restore(
    db: AsyncSession, archive: ParsedArchive, plan: RestorePlan, *, now: Optional[datetime] = None
) -> int:
    """Write the chat, its messages and its unexpired tool results. The
    caller commits. Returns the number of tool results restored."""
    if not plan.ok:
        raise ValueError("refusing to restore: " + "; ".join(plan.conflicts))
    now = now or datetime.now(UTC)
    c = archive.chat
    db.add(
        Chat(
            id=uuid.UUID(c["id"]),
            user_id=plan.owner_user_id,
            agent_id=plan.agent_id,
            agent_namespace=c.get("agent_namespace"),
            agent_name=c.get("agent_name"),
            title=c.get("title") or "",
            archived=bool(c.get("archived")),
            expires_at=_dt(c.get("expires_at")),
            session_key=c.get("session_key"),
            job_timeout=c.get("job_timeout"),
            keep_alive=c.get("keep_alive"),
            chat_metadata=c.get("chat_metadata"),
            created_at=_dt(c.get("created_at")),
            # A full retention period again, not the next run's first pick.
            updated_at=now,
        )
    )
    await db.flush()
    for m in archive.messages:
        db.add(
            Message(
                id=uuid.UUID(m["id"]),
                chat_id=uuid.UUID(c["id"]),
                role=m["role"],
                content=m.get("content"),
                tool_calls=m.get("tool_calls"),
                tool_call_id=m.get("tool_call_id"),
                name=m.get("name"),
                created_at=_dt(m["created_at"]),
            )
        )
    await db.flush()

    restored = 0
    for r in archive.tool_results:
        expires = _dt(r.get("expires_at"))
        if expires is None or expires <= now:
            continue
        try:
            # Its month's partition may be gone: that one is skipped, not fatal.
            async with db.begin_nested():
                db.add(
                    ToolCallResult(
                        id=uuid.UUID(r["id"]),
                        tool_call_id=r["tool_call_id"],
                        chat_id=uuid.UUID(c["id"]),
                        user_id=plan.owner_user_id,
                        tool_name=r["tool_name"],
                        arguments=r.get("arguments"),
                        result=r.get("result"),
                        result_size=r.get("result_size"),
                        status_code=r.get("status_code"),
                        duration_ms=r.get("duration_ms"),
                        source=r["source"],
                        created_at=_dt(r["created_at"]),
                        expires_at=expires,
                    )
                )
                await db.flush()
            restored += 1
        except Exception:
            plan.notes.append(f"tool result {r['id']} could not be restored (its partition is gone)")
    return restored
