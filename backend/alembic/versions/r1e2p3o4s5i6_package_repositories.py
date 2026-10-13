"""package_repositories + packages.source_repository

Package repositories (ADR 2026-10-13-package-repositories): URLs serving an
index.json of packages, refreshed by Sinas, from which packages are
installed and updates found. packages.source_repository records which
repository a package was installed from. Additive.

Revision ID: r1e2p3o4s5i6
Revises: m5s6t7a8t9s0
Create Date: 2026-10-13
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "r1e2p3o4s5i6"
down_revision = "m5s6t7a8t9s0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "package_repositories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("auth", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("index", sa.JSON(), nullable=True),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("managed_by", sa.String(255), nullable=True),
        sa.Column("config_name", sa.String(255), nullable=True),
        sa.Column("config_checksum", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_package_repositories_name", "package_repositories", ["name"], unique=True)
    op.add_column("packages", sa.Column("source_repository", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("packages", "source_repository")
    op.drop_index("ix_package_repositories_name", table_name="package_repositories")
    op.drop_table("package_repositories")
