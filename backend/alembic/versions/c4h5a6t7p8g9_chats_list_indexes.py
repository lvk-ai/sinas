"""chats_list_indexes: index GET /chats — chats by (user_id, updated_at, id), messages by (chat_id, created_at)

GET /chats aggregated max(created_at) over every message in the database to
find each chat's last message, whoever asked and however few chats they
had, then returned all of the caller's chats in one response. It now pages
on (updated_at, id) and looks up the last message per returned chat only.

ix_chats_user_id_updated_at_id serves the page: the caller's chats in
(updated_at, id) order, scanned backward for newest first, with the keyset
condition (updated_at, id) < cursor as an index condition.
ix_messages_chat_id_created_at turns each chat's max(created_at) into a
single backward index probe; it also orders a chat's messages for
GET /chats/{id}.

Both are built CONCURRENTLY, outside the migration transaction, so writes
are not blocked while they build; that is why this revision holds nothing
else (see c2u3s4i5d6x7). A build that fails leaves an INVALID index behind,
so each is dropped first if present: rerunning this revision is safe.

Benchmarked (EXPLAIN (ANALYZE, BUFFERS)) on 2M messages over 50k chats, as
for m5s6t7a8t9s0: a throwaway database seeded with generate_series, one
user with 40k chats and others with ~500. The old query took 0.20-0.25 s
for any caller (it read all 2M index entries); a 50-chat page now takes
0.3-0.5 ms, and the unpaginated list of the 40k-chat user 66-74 ms.

Revision ID: c4h5a6t7p8g9
Revises: s5e6t7u8p9l0
Create Date: 2026-10-18
"""

from alembic import op

revision = "c4h5a6t7p8g9"
down_revision = "s5e6t7u8p9l0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_chats_user_id_updated_at_id")
        op.execute(
            "CREATE INDEX CONCURRENTLY ix_chats_user_id_updated_at_id "
            "ON chats (user_id, updated_at, id)"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_messages_chat_id_created_at")
        op.execute(
            "CREATE INDEX CONCURRENTLY ix_messages_chat_id_created_at "
            "ON messages (chat_id, created_at)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_messages_chat_id_created_at")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_chats_user_id_updated_at_id")
