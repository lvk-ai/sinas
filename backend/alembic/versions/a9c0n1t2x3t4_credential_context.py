"""credential context: refresh_tokens.context, api_keys.context

The attribution context a session (refresh token) or an API key carries
(app/providers/attribution.py). Nullable columns without defaults:
metadata-only, no table rewrite.

Revision ID: a9c0n1t2x3t4
Revises: f1v2s3r4c5h6
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op

revision = "a9c0n1t2x3t4"
down_revision = "f1v2s3r4c5h6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("refresh_tokens", sa.Column("context", sa.JSON(), nullable=True))
    op.add_column("api_keys", sa.Column("context", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "context")
    op.drop_column("refresh_tokens", "context")
