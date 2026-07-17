"""pending_delegations table for suspend-on-delegate (issue #90)

Also serves as the merge point for the two heads dev grew from
b1a2t3c4h5e6 (0a1u2t3h4t5k connector-oauth and l1l2m3u4s5g6 llm-usage) —
without a merge, `alembic upgrade head` fails on a multi-head graph.

Revision ID: d1e2l3g4t5e6
Revises: 0a1u2t3h4t5k, l1l2m3u4s5g6
Create Date: 2026-07-14
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d1e2l3g4t5e6"
down_revision = ("0a1u2t3h4t5k", "l1l2m3u4s5g6")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_delegations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "chat_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chats.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.String(255), nullable=False),
        sa.Column("pending", sa.JSON(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=False),
        sa.Column("remaining", sa.Integer(), nullable=False),
        sa.Column("conversation_context", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_pending_delegations_chat_id", "pending_delegations", ["chat_id"])
    op.create_index("ix_pending_delegations_user_id", "pending_delegations", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_pending_delegations_user_id", table_name="pending_delegations")
    op.drop_index("ix_pending_delegations_chat_id", table_name="pending_delegations")
    op.drop_table("pending_delegations")
