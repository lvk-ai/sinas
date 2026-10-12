"""mcp_tasks: MCP tool calls answered with a task, polled in the background

Backs MCP Tasks (the io.modelcontextprotocol/tasks extension, MCP
2026-07-28): a task still running when the inline wait ends is kept here so
the agent worker can poll it and deliver its result into the chat, across
restarts. Additive: one new table.

Revision ID: m2t3a4s5k6s7
Revises: b1g2w3a4k5e6
Create Date: 2026-10-12
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "m2t3a4s5k6s7"
down_revision = "b1g2w3a4k5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("chat_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("mcp_server_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("server_task_id", sa.String(512), nullable=False),
        sa.Column("tool_name", sa.String(255), nullable=False),
        sa.Column("model_tool_name", sa.String(512), nullable=False),
        sa.Column("tool_call_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("status_message", sa.Text(), nullable=True),
        sa.Column("poll_interval_ms", sa.Integer(), nullable=True),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_requests", sa.JSON(), nullable=True),
        sa.Column("delivered_input_keys", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("answered_input_keys", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("pending_deliveries", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("api_key_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_mcp_tasks_chat_id", "mcp_tasks", ["chat_id"])
    op.create_index("ix_mcp_tasks_user_id", "mcp_tasks", ["user_id"])
    op.create_index("ix_mcp_tasks_mcp_server_id", "mcp_tasks", ["mcp_server_id"])
    op.create_index("ix_mcp_tasks_next_poll_at", "mcp_tasks", ["next_poll_at"])


def downgrade() -> None:
    op.drop_index("ix_mcp_tasks_next_poll_at", table_name="mcp_tasks")
    op.drop_index("ix_mcp_tasks_mcp_server_id", table_name="mcp_tasks")
    op.drop_index("ix_mcp_tasks_user_id", table_name="mcp_tasks")
    op.drop_index("ix_mcp_tasks_chat_id", table_name="mcp_tasks")
    op.drop_table("mcp_tasks")
