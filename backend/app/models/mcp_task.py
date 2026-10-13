"""MCP tasks: tool calls an MCP server answered with a task handle.

A server may answer `tools/call` with a task instead of a result (the
`io.modelcontextprotocol/tasks` extension, MCP 2026-07-28). One that is still
running when the inline wait ends is kept here and polled by the agent
worker (`mcp_tasks.poll_due_tasks`); its result is delivered into the chat
when it lands. The spec asks clients to keep task ids durably, so polling
survives a restart.
"""
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at, updated_at, uuid_pk


class McpTask(Base):
    __tablename__ = "mcp_tasks"

    id: Mapped[uuid_pk]
    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    mcp_server_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The server's own task id. It may double as a bearer token for the
    # server's state, so it stays here: the agent sees this row's id.
    server_task_id: Mapped[str] = mapped_column(String(512), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)  # the MCP tool
    model_tool_name: Mapped[str] = mapped_column(String(512), nullable=False)  # as the agent called it
    tool_call_id: Mapped[Optional[str]] = mapped_column(String(255))
    # working | input_required | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    status_message: Mapped[Optional[str]] = mapped_column(Text)
    poll_interval_ms: Mapped[Optional[int]] = mapped_column(Integer)
    # When the sweep looks next (a poll, or a delivery to retry); NULL once
    # the task is finished and everything is delivered.
    next_poll_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    # From the server's ttlMs: past it the server may have dropped the task.
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    input_requests: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    # Input request keys already put in front of the agent, and answered.
    delivered_input_keys: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    answered_input_keys: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # The final result as the agent got it (mapped like any tool result).
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    # Messages for the chat not yet accepted by it (an outbox): written in
    # the same commit as the state change they report, removed once
    # delivered, retried by the sweep until then.
    pending_deliveries: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # Consecutive polls that could not reach the server.
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # The API key the calling turn acted through, so the turn the result
    # starts keeps acting through it (as chat wakeups do).
    api_key_id: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[created_at]
    updated_at: Mapped[updated_at]
