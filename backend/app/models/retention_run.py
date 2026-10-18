"""Chat retention runs: the audit record of every real retention run.

One row per run (scheduled, "Run now", or the CLI), written when the run
is queued or starts and completed when it ends: who or what started it, the
policy it applied, and how many chats it archived, deleted, skipped (and
why) and failed on. Never any chat content, title or message. Dry runs
change nothing and aren't recorded. See docs/design/chat-retention.md.
"""
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import GUID, Base, created_at, uuid_pk

QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
PARTIAL = "partial"  # a budget ended it with chats left: the next tick continues
FAILED = "failed"
REFUSED = "refused"  # misconfigured (e.g. archive with no destination): nothing done
# Stopped without finishing (worker cancelled, job timed out, process died):
# its counts are what it got done; the next tick continues from its cursor.
INTERRUPTED = "interrupted"

TRIGGERS = ("schedule", "api", "cli")


class ChatRetentionRun(Base):
    __tablename__ = "chat_retention_runs"
    __table_args__ = (Index("ix_chat_retention_runs_created_at", "created_at"),)

    id: Mapped[uuid_pk]
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)  # schedule | api | cli
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Not a foreign key: the audit row outlives the user.
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID())
    actor_email: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[created_at]
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # The policy applied: enabled, days, action, destination (never
    # credentials), agent overrides.
    policy: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    # {"candidates", "archived", "deleted", "messages_deleted", "bytes_archived",
    #  "skipped": {reason: n}, "failed": {stage: n}, "more_remaining": bool}
    counts: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    # {"namespace/name" | "(no agent)": {"archived": n, "deleted": n}}
    per_agent: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    error: Mapped[Optional[str]] = mapped_column(Text)
    # The last chat the run looked at, in selection order (chats.updated_at,
    # chats.id): a continuation of a partial or interrupted run starts after
    # it, so chats that keep failing or being skipped can't starve the rest.
    cursor_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    cursor_chat_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID())
