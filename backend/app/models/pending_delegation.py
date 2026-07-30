"""Pending agent-to-agent delegation tracking (suspend-on-delegate, issue #90)."""
import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at, uuid_pk


class PendingDelegation(Base):
    """A parent agent turn suspended while its delegated sub-agents run.

    Created when `agent_delegate_mode="suspend"` and an LLM turn contains
    `call_agent_*` tool calls: the parent job ends (freeing its worker slot)
    after enqueueing the children; each child reports back on completion, and
    the last one enqueues a delegate-resume job that continues the parent
    conversation. One row per suspended tool round (covers all delegate calls
    in that round). Mirrors `PendingToolApproval`'s checkpoint pattern.
    """

    __tablename__ = "pending_delegations"

    id: Mapped[uuid_pk]
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)

    # Parent's SSE stream channel — the resume job keeps publishing here.
    channel_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # {tool_call_id: {"sub_chat_id": ..., "agent": ...}} still outstanding.
    pending: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # {tool_call_id: result content} collected from finished children.
    results: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # Outstanding count; decremented atomically as children finish. 0 → resume.
    remaining: Mapped[int] = mapped_column(Integer, nullable=False)

    # Provider/model/tools/etc. needed by the follow-up LLM turn — same shape
    # the approval flow stashes (see PendingToolApproval.conversation_context).
    conversation_context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    created_at: Mapped[created_at]
