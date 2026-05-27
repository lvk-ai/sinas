"""add callback_status fields to executions

Revision ID: c1a2l3b4k5s6
Revises: p1r2e3s4t5k6
Create Date: 2026-05-14
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c1a2l3b4k5s6"
down_revision = "p1r2e3s4t5k6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column("callback_status", sa.String(20), nullable=True),
    )
    op.add_column(
        "executions",
        sa.Column("callback_response_code", sa.Integer, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("executions", "callback_response_code")
    op.drop_column("executions", "callback_status")
