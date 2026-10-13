"""chat_wakeups: timers and watches that deliver a message into a chat later

Backs the `background` system tool (ADR 2026-10-11-background-jobs-and-
wakeups): an agent can schedule its own check-in (`timer`) or hand an
external system a callback URL (`watch`); each delivery lands in the chat
as a message. Additive: one new table.

Revision ID: b1g2w3a4k5e6
Revises: m1c2p3s4r5v6
Create Date: 2026-10-11
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b1g2w3a4k5e6"
down_revision = "m1c2p3s4r5v6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_wakeups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("chat_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fired_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("api_key_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_chat_wakeups_chat_id", "chat_wakeups", ["chat_id"])
    op.create_index("ix_chat_wakeups_user_id", "chat_wakeups", ["user_id"])
    op.create_index("ix_chat_wakeups_due_at", "chat_wakeups", ["due_at"])
    op.create_index("ix_chat_wakeups_token", "chat_wakeups", ["token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_chat_wakeups_token", table_name="chat_wakeups")
    op.drop_index("ix_chat_wakeups_due_at", table_name="chat_wakeups")
    op.drop_index("ix_chat_wakeups_user_id", table_name="chat_wakeups")
    op.drop_index("ix_chat_wakeups_chat_id", table_name="chat_wakeups")
    op.drop_table("chat_wakeups")
