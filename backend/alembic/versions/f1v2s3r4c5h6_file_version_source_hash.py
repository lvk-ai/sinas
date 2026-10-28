"""file_versions.source_hash_sha256: hash of the bytes as uploaded

A collection's content filter may rewrite an upload before it is stored, so
hash_sha256 (of the stored bytes) can't tell whether a retried upload carries
the same content. on_conflict='return_existing' compares against this too.

Nullable without a default: metadata-only on Postgres 11+, no table rewrite.
Existing rows stay NULL, which means "same as hash_sha256".

Revision ID: f1v2s3r4c5h6
Revises: a8t9r0i1b2u3
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op

revision = "f1v2s3r4c5h6"
down_revision = "a8t9r0i1b2u3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "file_versions",
        sa.Column("source_hash_sha256", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("file_versions", "source_hash_sha256")
