"""chats_updated_at_index: index chats by (updated_at, id) for chat retention

Retention selects chats idle past the shortest retention period, oldest
first, keyset-paginated on (updated_at, id). The existing
ix_chats_user_id_updated_at_id leads with user_id, so it can't serve a
range over updated_at across all users; without this index every run would
scan the whole chats table.

Built CONCURRENTLY, outside the migration transaction, so writes are not
blocked while it builds; that is why this revision holds nothing else (see
c4h5a6t7p8g9). A failed build leaves an INVALID index behind, so it is
dropped first if present: rerunning this revision is safe.

Revision ID: r8e9t0i1d2x3
Revises: r7e8t9e0n1t2
Create Date: 2026-10-18
"""

from alembic import op

revision = "r8e9t0i1d2x3"
down_revision = "r7e8t9e0n1t2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_chats_updated_at_id")
        op.execute("CREATE INDEX CONCURRENTLY ix_chats_updated_at_id ON chats (updated_at, id)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_chats_updated_at_id")
