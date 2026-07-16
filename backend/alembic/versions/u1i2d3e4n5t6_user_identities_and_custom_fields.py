"""Add user_identities table and users.custom_fields; drop unused external_* columns

Revision ID: u1i2d3e4n5t6
Revises: b1a2t3c4h5e6
Create Date: 2026-07-16
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "u1i2d3e4n5t6"
down_revision = "b1a2t3c4h5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("identity_metadata", sa.JSON(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"])
    op.create_index(
        "ix_user_identity_provider_subject",
        "user_identities",
        ["provider", "subject"],
        unique=True,
    )

    op.add_column("users", sa.Column("custom_fields", sa.JSON(), nullable=True))

    # Preserve any existing external identities (columns were never written by
    # application code, but data may exist from manual/DB-level provisioning)
    op.execute(
        """
        INSERT INTO user_identities
            (id, user_id, provider, subject, identity_metadata, last_synced_at, created_at, updated_at)
        SELECT gen_random_uuid(), id, 'external', external_user_id, external_metadata,
               last_external_sync, now(), now()
        FROM users
        WHERE external_user_id IS NOT NULL
        """
    )

    op.drop_index("ix_users_external_user_id", table_name="users")
    op.drop_column("users", "last_external_sync")
    op.drop_column("users", "external_metadata")
    op.drop_column("users", "external_user_id")


def downgrade() -> None:
    op.add_column("users", sa.Column("external_user_id", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("external_metadata", sa.JSON(), nullable=True))
    op.add_column("users", sa.Column("last_external_sync", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_users_external_user_id", "users", ["external_user_id"], unique=True)

    op.execute(
        """
        UPDATE users u
        SET external_user_id = ui.subject,
            external_metadata = ui.identity_metadata,
            last_external_sync = ui.last_synced_at
        FROM user_identities ui
        WHERE ui.user_id = u.id AND ui.provider = 'external'
        """
    )

    op.drop_column("users", "custom_fields")
    op.drop_index("ix_user_identity_provider_subject", table_name="user_identities")
    op.drop_index("ix_user_identities_user_id", table_name="user_identities")
    op.drop_table("user_identities")
