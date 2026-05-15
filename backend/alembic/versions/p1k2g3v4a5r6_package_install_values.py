"""Add values column to packages for install-time variables

Revision ID: p1k2g3v4a5r6
Revises: r1e2m3g4r5p6
Create Date: 2026-04-29
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "p1k2g3v4a5r6"
down_revision = "r1e2m3g4r5p6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "packages",
        sa.Column(
            "values",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("packages", "values")
