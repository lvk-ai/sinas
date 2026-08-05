"""add agent targets and raw response mode to webhooks

Revision ID: w2a3b4t5g6t7
Revises: c1a2c3h4e5t6
Create Date: 2026-07-27
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "w2a3b4t5g6t7"
down_revision = "c1a2c3h4e5t6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "webhooks",
        sa.Column("target_type", sa.String(10), nullable=False, server_default="function"),
    )
    op.add_column("webhooks", sa.Column("agent_namespace", sa.String(255), nullable=True))
    op.add_column("webhooks", sa.Column("agent_name", sa.String(255), nullable=True))
    op.add_column("webhooks", sa.Column("message_template", sa.Text, nullable=True))
    op.add_column("webhooks", sa.Column("session_key_template", sa.String(500), nullable=True))
    # function_name is no longer required (agent-target webhooks have no function)
    op.alter_column("webhooks", "function_name", existing_type=sa.String(255), nullable=True)


def downgrade() -> None:
    op.alter_column("webhooks", "function_name", existing_type=sa.String(255), nullable=False)
    op.drop_column("webhooks", "session_key_template")
    op.drop_column("webhooks", "message_template")
    op.drop_column("webhooks", "agent_name")
    op.drop_column("webhooks", "agent_namespace")
    op.drop_column("webhooks", "target_type")
