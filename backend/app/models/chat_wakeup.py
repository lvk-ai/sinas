"""Chat wakeups: timers and watches that deliver a message into a chat later.

A `timer` fires once at `due_at` (the agent's own "check back in N minutes");
a `watch` is a callback URL (`token`) external systems POST to, each delivery
landing in the chat. Both are created by the agent through the `background`
system tool and delivered by `chat_wakeups.deliver_to_chat`.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at, uuid_pk

TIMER = "timer"
WATCH = "watch"


class ChatWakeup(Base):
    __tablename__ = "chat_wakeups"

    id: Mapped[uuid_pk]
    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text)
    # timer: when to deliver; set once delivered.
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # watch: the secret path segment external callers POST to.
    token: Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    fired_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_fired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    cancelled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # The API key the creating turn acted through, so the woken turn keeps
    # acting through it (mirrors the deferred-round checkpoint).
    api_key_id: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[created_at]
