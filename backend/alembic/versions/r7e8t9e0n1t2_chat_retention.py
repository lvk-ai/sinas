"""chat_retention: agents.retention_days and the chat_retention_runs audit table

agents.retention_days is the per-agent override of CHAT_RETENTION_DAYS
(NULL = the deployment default, 0 = keep forever). Nullable with no
default, so adding it rewrites nothing and changes no agent's behavior.

chat_retention_runs holds one row per real retention run: trigger, actor,
policy, counts. See docs/design/chat-retention.md. The index on chats that
selection reads is built concurrently in the next revision.

Revision ID: r7e8t9e0n1t2
Revises: c4h5a6t7p8g9
Create Date: 2026-10-18
"""

import sqlalchemy as sa

from alembic import op
from app.models.base import GUID

revision = "r7e8t9e0n1t2"
down_revision = "c4h5a6t7p8g9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("retention_days", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_agents_retention_days_non_negative", "agents", "retention_days IS NULL OR retention_days >= 0"
    )
    op.create_table(
        "chat_retention_runs",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("actor_user_id", GUID(), nullable=True),
        sa.Column("actor_email", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("policy", sa.JSON(), nullable=True),
        sa.Column("counts", sa.JSON(), nullable=True),
        sa.Column("per_agent", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_chat_retention_runs_created_at", "chat_retention_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_chat_retention_runs_created_at", table_name="chat_retention_runs")
    op.drop_table("chat_retention_runs")
    op.drop_constraint("ck_agents_retention_days_non_negative", "agents", type_="check")
    op.drop_column("agents", "retention_days")
