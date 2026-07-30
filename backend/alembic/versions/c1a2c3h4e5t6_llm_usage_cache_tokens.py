"""add prompt-cache token columns to llm_usage

Revision ID: c1a2c3h4e5t6
Revises: d1e2l3g4t5e6
Create Date: 2026-07-17
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c1a2c3h4e5t6"
down_revision = "d1e2l3g4t5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_usage",
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "llm_usage",
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("llm_usage", "cache_write_tokens")
    op.drop_column("llm_usage", "cache_read_tokens")
