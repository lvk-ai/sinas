"""add connector_oauth_tokens table (per-user authorization-code grant)

Also merges the two open migration heads (b1a2t3c4h5e6, 20260131_states) into one.

Revision ID: 0a1u2t3h4t5k
Revises: b1a2t3c4h5e6, 20260131_states
Create Date: 2026-07-03
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0a1u2t3h4t5k"
down_revision = ("b1a2t3c4h5e6", "20260131_states")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connector_oauth_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "connector_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("connectors.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("encrypted_access_token", sa.Text(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("token_type", sa.String(length=40), nullable=False, server_default="Bearer"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("connector_id", "user_id", name="uq_connector_oauth_token_connector_user"),
    )


def downgrade() -> None:
    op.drop_table("connector_oauth_tokens")
