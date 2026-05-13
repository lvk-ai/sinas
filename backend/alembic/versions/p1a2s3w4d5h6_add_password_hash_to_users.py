"""Add password_hash column to users for password auth mode

Revision ID: p1a2s3w4d5h6
Revises: p1k2g3v4a5r6
Create Date: 2026-05-05
"""
import sqlalchemy as sa
from alembic import op

revision = "p1a2s3w4d5h6"
down_revision = "p1k2g3v4a5r6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("password_hash", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "password_hash")
