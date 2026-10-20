"""functions.max_retries: opt-in retries of a queued function's own failures

0 (the default, and every existing function) keeps today's behaviour: a
failure of the function's code is final. See issue #135.

Revision ID: r1e2t3r4y5f6
Revises: a9f1n2s3h4r5
Create Date: 2026-10-20
"""
import sqlalchemy as sa

from alembic import op

revision = "r1e2t3r4y5f6"
down_revision = "a9f1n2s3h4r5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "functions",
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("functions", "max_retries")
