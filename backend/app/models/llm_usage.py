"""Per-call LLM token usage records."""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID


class LLMUsage(Base):
    """One row per LLM API call (completion or stream), for usage accounting.

    Intentionally has no foreign keys: rows must survive deletion of the
    chat, message or user they were attributed to. chat_id/message_id are
    NULL for calls made outside the chat pipeline (e.g. the OpenAI-compat
    adapter's passthrough mode).
    """

    __tablename__ = "llm_usage"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID(), nullable=True)
    chat_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID(), nullable=True, index=True)
    message_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID(), nullable=True)

    # "namespace/name" of the agent handling the chat, if any
    agent: Mapped[Optional[str]] = mapped_column(String(511), nullable=True)
    # Call site: "chat" | "openai_adapter" | "openai_adapter_passthrough" | ...
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Configured provider name and its type (openai, azure, anthropic, ...)
    provider_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    provider_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    streamed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Set when the call raised; token counts may then be 0/partial
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_llm_usage_user_created", "user_id", "created_at"),
        Index("idx_llm_usage_created", "created_at"),
    )
