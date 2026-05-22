"""Tool result store — persistent storage for tool call results."""
import json
import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.tool_call_result import ToolCallResult

logger = logging.getLogger(__name__)

# Max result size before truncation (bytes)
MAX_RESULT_SIZE = settings.tool_result_max_size


async def save_tool_result(
    db: AsyncSession,
    tool_call_id: str,
    tool_name: str,
    arguments: Optional[dict[str, Any]],
    result: Any,
    user_id: str,
    chat_id: Optional[str] = None,
    source: str = "agent",
    status_code: Optional[int] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """Save a tool call result. Fire-and-forget — errors are logged, not raised."""
    try:


        # Calculate result size
        result_json = result if isinstance(result, dict) else {"value": result}
        result_str = json.dumps(result_json, default=str)
        result_size = len(result_str.encode("utf-8"))

        # Truncate large results
        if result_size > MAX_RESULT_SIZE:
            result_json = {
                "_truncated": True,
                "_original_size": result_size,
                "_preview": result_str[:1000] + "...",
            }

        retention_days = settings.tool_result_retention_days
        expires_at = datetime.now(timezone.utc) + timedelta(days=retention_days)

        record = ToolCallResult(
            id=uuid.uuid4(),
            tool_call_id=tool_call_id,
            chat_id=uuid.UUID(chat_id) if chat_id else None,
            user_id=uuid.UUID(user_id) if isinstance(user_id, str) else user_id,
            tool_name=tool_name,
            arguments=arguments,
            result=result_json,
            result_size=result_size,
            status_code=status_code,
            duration_ms=duration_ms,
            source=source,
            created_at=datetime.now(timezone.utc),
            expires_at=expires_at,
        )

        db.add(record)
        await db.commit()

    except Exception as e:
        logger.debug(f"Failed to save tool result for {tool_call_id}: {e}")
        try:
            await db.rollback()
        except Exception:
            pass


async def get_tool_result(
    db: AsyncSession,
    tool_call_id: str,
    user_id: str,
    chat_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Retrieve a tool result by tool_call_id.

    The `messages` table is the source of truth — tool-role rows are
    committed synchronously as part of the chat flow, before any
    subsequent LLM call. Reading from messages is always race-free.

    The `tool_call_results` table is a write-behind analytics cache (per-
    user, longer-lived, lets you query across chats). It must NOT be
    load-bearing for the runtime `retrieve_tool_result` path.
    """
    from app.models.chat import Message
    from app.models.tool_call_result import ToolCallResult

    # 1. Primary: messages row, scoped to this chat. The chat scope is the
    #    natural correctness boundary — the agent is asking about an id it
    #    saw in its own conversation.
    if chat_id:
        msg = (
            await db.execute(
                select(Message).where(
                    and_(
                        Message.tool_call_id == tool_call_id,
                        Message.chat_id == uuid.UUID(chat_id),
                        Message.role == "tool",
                    )
                )
            )
        ).scalar_one_or_none()
        if msg:
            content = msg.content
            try:
                result_value: Any = (
                    json.loads(content) if isinstance(content, str) else content
                )
            except (json.JSONDecodeError, TypeError):
                result_value = {"raw": content}
            return {
                "tool_call_id": tool_call_id,
                "tool_name": msg.name or "unknown",
                "arguments": None,
                "result": result_value,
                "status_code": None,
                "duration_ms": None,
                "source": "messages",
                "created_at": msg.created_at.isoformat() if msg.created_at else None,
            }

    # 2. Fallback: user-scoped analytics cache (for the rare case an agent
    #    legitimately wants to look up a result from another chat — e.g.,
    #    agent-to-agent handoff). Owns enrichment fields (status_code,
    #    duration_ms) that the messages row doesn't carry.
    record = (
        await db.execute(
            select(ToolCallResult).where(
                and_(
                    ToolCallResult.tool_call_id == tool_call_id,
                    ToolCallResult.user_id == uuid.UUID(user_id),
                )
            )
        )
    ).scalar_one_or_none()
    if record:
        return {
            "tool_call_id": record.tool_call_id,
            "tool_name": record.tool_name,
            "arguments": record.arguments,
            "result": record.result,
            "status_code": record.status_code,
            "duration_ms": record.duration_ms,
            "source": record.source,
            "created_at": record.created_at.isoformat(),
        }

    return None


async def list_chat_tool_call_ids(
    db: AsyncSession, chat_id: str, limit: int = 25
) -> list[dict[str, str]]:
    """Return recent tool_call_ids for a chat — used to build a helpful
    error response when `get_tool_result` finds nothing (agent likely
    hallucinated the id). Most recent first."""
    from app.models.chat import Message

    rows = (
        await db.execute(
            select(Message.tool_call_id, Message.name, Message.created_at)
            .where(Message.chat_id == uuid.UUID(chat_id))
            .where(Message.role == "tool")
            .where(Message.tool_call_id.isnot(None))
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        {"tool_call_id": tcid, "tool_name": name or "unknown"}
        for tcid, name, _ in rows
    ]


async def ensure_partitions(db: AsyncSession) -> None:
    """Create partitions for the next 2 months if they don't exist."""
    now = datetime.now(timezone.utc)
    for month_offset in range(3):
        year = now.year
        month = now.month + month_offset
        if month > 12:
            month -= 12
            year += 1
        next_month = month + 1
        next_year = year
        if next_month > 12:
            next_month = 1
            next_year += 1

        partition_name = f"tool_call_results_{year}_{month:02d}"
        from_date = f"{year}-{month:02d}-01"
        to_date = f"{next_year}-{next_month:02d}-01"

        try:
            await db.execute(text(
                f"CREATE TABLE IF NOT EXISTS {partition_name} "
                f"PARTITION OF tool_call_results "
                f"FOR VALUES FROM ('{from_date}') TO ('{to_date}')"
            ))
            await db.commit()
        except Exception:
            await db.rollback()


async def cleanup_expired_partitions(db: AsyncSession) -> int:
    """Drop partitions older than retention period. Returns count of dropped partitions."""
    retention_days = settings.tool_result_retention_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days + 31)  # +31 for full month buffer

    dropped = 0
    # List partitions
    result = await db.execute(text(
        "SELECT tablename FROM pg_tables WHERE tablename LIKE 'tool_call_results_%' AND schemaname = 'public'"
    ))
    for row in result.fetchall():
        table_name = row[0]
        # Extract year_month from partition name
        try:
            parts = table_name.replace("tool_call_results_", "").split("_")
            year, month = int(parts[0]), int(parts[1])
            partition_date = datetime(year, month, 1, tzinfo=timezone.utc)
            if partition_date < cutoff:
                await db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
                await db.commit()
                logger.info(f"Dropped expired partition: {table_name}")
                dropped += 1
        except (ValueError, IndexError):
            continue

    return dropped
