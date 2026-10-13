"""messages_created_at_indexes: index messages by created_at for the dashboard stats

GET /messages/stats counts the messages of the last N days. Without an
index on created_at every one of its queries scanned the whole messages
table. ix_messages_created_at carries chat_id and role so the grouped
counts are index-only; the partial ix_messages_created_at_tool_calls holds
only the messages with tool calls, for the tool-call count.

Both are built CONCURRENTLY, outside the migration transaction, so writes
to messages are not blocked while they build. A build that fails leaves an
INVALID index behind, so each is dropped first if present.

Revision ID: m5s6t7a8t9s0
Revises: m2t3a4s5k6s7
Create Date: 2026-10-13
"""
from alembic import op

revision = "m5s6t7a8t9s0"
down_revision = "m2t3a4s5k6s7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_messages_created_at")
        op.execute(
            "CREATE INDEX CONCURRENTLY ix_messages_created_at "
            "ON messages (created_at) INCLUDE (chat_id, role)"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_messages_created_at_tool_calls")
        op.execute(
            "CREATE INDEX CONCURRENTLY ix_messages_created_at_tool_calls "
            "ON messages (created_at) INCLUDE (chat_id) WHERE tool_calls IS NOT NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_messages_created_at_tool_calls")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_messages_created_at")
