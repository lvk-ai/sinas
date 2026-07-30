"""add is_active column to users

Revision ID: u1s2r3a4c5t6
Revises: b1a2t3c4h5e6
Create Date: 2026-07-16
"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "u1s2r3a4c5t6"
down_revision = "b1a2t3c4h5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.create_index("ix_users_is_active", "users", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_users_is_active", table_name="users")
    op.drop_column("users", "is_active")
