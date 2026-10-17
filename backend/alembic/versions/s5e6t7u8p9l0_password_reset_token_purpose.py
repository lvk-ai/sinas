"""password_reset_tokens.purpose: a link, or the setup session one was traded for

Setup links (`<console>/ui?token=...`) are spent for a short-lived setup
session stored in the same table. The two must not be interchangeable: a
session handed to POST /auth/setup-link/exchange would mint another session,
indefinitely. Existing rows are links (admin reset links).

Revision ID: s5e6t7u8p9l0
Revises: c3p4r5f6a7m8
Create Date: 2026-10-17
"""
import sqlalchemy as sa
from alembic import op

revision = "s5e6t7u8p9l0"
down_revision = "c3p4r5f6a7m8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "password_reset_tokens",
        sa.Column("purpose", sa.String(16), nullable=False, server_default="link"),
    )
    op.create_check_constraint(
        "ck_password_reset_tokens_purpose",
        "password_reset_tokens",
        "purpose IN ('link', 'setup_session')",
    )


def downgrade() -> None:
    # Sessions would turn into links on downgrade: drop them instead.
    op.execute("DELETE FROM password_reset_tokens WHERE purpose = 'setup_session'")
    op.drop_constraint("ck_password_reset_tokens_purpose", "password_reset_tokens", type_="check")
    op.drop_column("password_reset_tokens", "purpose")
