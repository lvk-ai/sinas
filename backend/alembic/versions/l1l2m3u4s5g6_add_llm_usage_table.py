"""add llm_usage table for per-call token tracking

Revision ID: l1l2m3u4s5g6
Revises: b1a2t3c4h5e6
Create Date: 2026-07-16
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "l1l2m3u4s5g6"
down_revision = "b1a2t3c4h5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("chat_id", UUID(as_uuid=True), nullable=True),
        sa.Column("message_id", UUID(as_uuid=True), nullable=True),
        sa.Column("agent", sa.String(511), nullable=True),
        sa.Column("source", sa.String(50), nullable=True),
        sa.Column("provider_name", sa.String(255), nullable=True),
        sa.Column("provider_type", sa.String(50), nullable=True),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("streamed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("idx_llm_usage_user_created", "llm_usage", ["user_id", "created_at"])
    op.create_index("idx_llm_usage_created", "llm_usage", ["created_at"])
    op.create_index(op.f("ix_llm_usage_chat_id"), "llm_usage", ["chat_id"])


def downgrade() -> None:
    op.drop_table("llm_usage")
