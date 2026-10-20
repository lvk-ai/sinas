"""llm_usage.finish_reason / finish_reason_raw: why each call stopped

finish_reason: normalized (stop, length, tool_calls, content_filter, error,
other); finish_reason_raw: the provider's own value. Nullable columns
without a default, so on Postgres 11+ they are added without rewriting
llm_usage (one row per LLM call, large) and it stays writable; rows recorded
before stay NULL. No index: the usage API only returns the value, it doesn't
filter or group by it.

Revision ID: a9f1n2s3h4r5
Revises: a8t9r0i1b2u3
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op

revision = "a9f1n2s3h4r5"
down_revision = "a8t9r0i1b2u3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_usage", sa.Column("finish_reason", sa.String(length=20), nullable=True))
    op.add_column("llm_usage", sa.Column("finish_reason_raw", sa.String(length=50), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_usage", "finish_reason_raw")
    op.drop_column("llm_usage", "finish_reason")
