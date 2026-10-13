"""Messages API endpoints for analytics and insights."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, cast, func, select, Date
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user_with_permissions, set_permission_used, via_api_key
from app.core.database import get_db
from app.core.permissions import check_permission
from app.models import Chat, Message, User
from app.models.llm_usage import LLMUsage
from app.services.message_service import strip_base64_data
from app.services.content_tokens import strip_render_tokens

router = APIRouter(prefix="/messages", tags=["messages"])


@router.get("")
async def list_messages(
    request: Request,
    agent: Optional[str] = Query(None, description="Filter by agent (namespace/name)"),
    role: Optional[str] = Query(None, description="Filter by role (user/assistant/tool/system)"),
    search: Optional[str] = Query(None, description="Search in content"),
    limit: int = Query(50, ge=1, le=1000, description="Max messages to return"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    current_user_data: tuple = Depends(get_current_user_with_permissions),
    db: AsyncSession = Depends(get_db),
):
    """
    List messages with filters for analytics/insights.

    Permissions:
    - sinas.executions.read:all - View all messages
    - sinas.executions.read:own - View only own messages
    """
    user_id, permissions = current_user_data

    # Check permissions
    has_all_permission = check_permission(permissions, "sinas.executions.read:all")
    has_own_permission = check_permission(permissions, "sinas.executions.read:own")

    if not has_all_permission and not has_own_permission:
        set_permission_used(request, "sinas.executions.read:own", has_perm=False)
        raise HTTPException(status_code=403, detail="Not authorized to view messages")

    # Build query
    query = select(Message).join(Chat, Message.chat_id == Chat.id)

    # Apply permission-based filtering
    if not has_all_permission:
        # User can only see their own messages
        query = query.join(User, Chat.user_id == User.id).where(Chat.user_id == user_id)
        set_permission_used(request, "sinas.executions.read:own")
    else:
        # Admin can see all
        query = query.join(User, Chat.user_id == User.id, isouter=True)
        set_permission_used(request, "sinas.executions.read:all")

    # Apply filters
    if agent:
        if "/" in agent:
            namespace, name = agent.split("/", 1)
            query = query.where(and_(Chat.agent_namespace == namespace, Chat.agent_name == name))
        else:
            query = query.where(Chat.agent_name == agent)

    if role:
        query = query.where(Message.role == role)

    if search:
        query = query.where(Message.content.ilike(f"%{search}%"))

    # Order by newest first
    query = query.order_by(Message.created_at.desc())

    # Get total count
    count_query = select(func.count()).select_from(Message).join(Chat, Message.chat_id == Chat.id)
    if not has_all_permission:
        count_query = count_query.where(Chat.user_id == user_id)
    if agent:
        if "/" in agent:
            namespace, name = agent.split("/", 1)
            count_query = count_query.where(
                and_(Chat.agent_namespace == namespace, Chat.agent_name == name)
            )
    if role:
        count_query = count_query.where(Message.role == role)
    if search:
        count_query = count_query.where(Message.content.ilike(f"%{search}%"))

    total_result = await db.execute(count_query)
    total = total_result.scalar()

    # Apply pagination
    query = query.limit(limit).offset(offset)

    # Execute query with chat and user info in a single query (avoid N+1)
    enriched_query = (
        query.add_columns(
            Chat.agent_namespace,
            Chat.agent_name,
            User.email.label("user_email"),
        )
    )

    result = await db.execute(enriched_query)
    rows = result.all()

    enriched_messages = []
    for row in rows:
        msg = row[0]  # Message object
        agent_namespace = row[1]
        agent_name = row[2]
        user_email = row[3]

        enriched_messages.append(
            {
                "id": str(msg.id),
                "chat_id": str(msg.chat_id),
                "role": msg.role,
                "content": (
                    strip_render_tokens(strip_base64_data(msg.content))
                    if via_api_key(request)
                    else strip_base64_data(msg.content)
                ),
                "tool_calls": msg.tool_calls,
                "tool_call_id": msg.tool_call_id,
                "created_at": msg.created_at.isoformat(),
                "chat": {
                    "agent_namespace": agent_namespace,
                    "agent_name": agent_name,
                },
                "user": {"email": user_email} if user_email else None,
            }
        )

    return {"messages": enriched_messages, "total": total, "limit": limit, "offset": offset}


@router.get("/stats")
async def get_message_stats(
    request: Request,
    days: int = Query(7, ge=1, le=90, description="Number of days to include"),
    current_user_data: tuple = Depends(get_current_user_with_permissions),
    db: AsyncSession = Depends(get_db),
):
    """
    Dashboard statistics — message counts, activity by day, agent usage, role distribution.
    Three queries (grouped counts, tool calls, LLM usage), each served by an
    index on created_at, so the window is read without scanning the tables.
    """
    user_id, permissions = current_user_data

    has_all = check_permission(permissions, "sinas.executions.read:all")
    has_own = check_permission(permissions, "sinas.executions.read:own")

    if not has_all and not has_own:
        set_permission_used(request, "sinas.executions.read:own", has_perm=False)
        raise HTTPException(status_code=403, detail="Not authorized to view message stats")

    set_permission_used(request, "sinas.executions.read:all" if has_all else "sinas.executions.read:own")

    since = datetime.now(timezone.utc) - timedelta(days=days)

    # Base filter
    base_filter = Message.created_at >= since
    if not has_all:
        base_filter = and_(base_filter, Chat.user_id == user_id)

    # One pass over the window for everything except the tool-call count:
    # message counts per (day, agent, role), folded below into the total, the
    # per-day series, the top agents and the role split. It reads only
    # ix_messages_created_at (created_at INCLUDE chat_id, role), never the
    # messages heap.
    day = cast(Message.created_at, Date)
    grouped_q = (
        select(
            day.label("day"),
            Chat.agent_namespace,
            Chat.agent_name,
            Message.role,
            func.count().label("count"),
        )
        .select_from(Message)
        .join(Chat, Message.chat_id == Chat.id)
        .where(base_filter)
        .group_by(day, Chat.agent_namespace, Chat.agent_name, Message.role)
    )
    total = 0
    by_day: dict[str, int] = defaultdict(int)
    by_agent: dict[str, int] = defaultdict(int)
    role_distribution: dict[str, int] = defaultdict(int)
    for r in (await db.execute(grouped_q)).all():
        total += r.count
        by_day[str(r.day)] += r.count
        by_agent[f"{r.agent_namespace}/{r.agent_name}"] += r.count
        role_distribution[r.role] += r.count
    activity_by_day = dict(sorted(by_day.items()))
    agent_usage = [
        {"agent": agent, "count": count}
        for agent, count in sorted(by_agent.items(), key=lambda kv: kv[1], reverse=True)[:10]
    ]

    # Tool calls are counted apart: tool_calls lives in the heap, and the
    # partial index ix_messages_created_at_tool_calls holds just those rows.
    tool_q = (
        select(func.count())
        .select_from(Message)
        .join(Chat, Message.chat_id == Chat.id)
        .where(and_(base_filter, Message.tool_calls.isnot(None)))
    )
    tool_calls = (await db.execute(tool_q)).scalar() or 0

    # LLM token usage over the same window (same read:all / read:own scoping).
    usage_filter = LLMUsage.created_at >= since
    if not has_all:
        usage_filter = and_(usage_filter, LLMUsage.user_id == user_id)
    usage_q = select(
        func.coalesce(func.sum(LLMUsage.prompt_tokens), 0),
        func.coalesce(func.sum(LLMUsage.completion_tokens), 0),
        func.coalesce(func.sum(LLMUsage.total_tokens), 0),
        func.coalesce(func.sum(LLMUsage.cache_read_tokens), 0),
        func.coalesce(func.sum(LLMUsage.cache_write_tokens), 0),
        func.count(),
    ).where(usage_filter)
    u_prompt, u_completion, u_total, u_cache_read, u_cache_write, u_calls = (
        await db.execute(usage_q)
    ).one()

    return {
        "total_messages": total,
        "tool_calls": tool_calls,
        "activity_by_day": activity_by_day,
        "agent_usage": agent_usage,
        "role_distribution": dict(role_distribution),
        "llm_prompt_tokens": int(u_prompt),
        "llm_completion_tokens": int(u_completion),
        "llm_total_tokens": int(u_total),
        "llm_cache_read_tokens": int(u_cache_read),
        "llm_cache_write_tokens": int(u_cache_write),
        "llm_calls": u_calls,
        "days": days,
    }
