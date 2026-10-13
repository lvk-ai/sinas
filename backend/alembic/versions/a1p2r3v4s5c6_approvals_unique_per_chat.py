"""pending_tool_approvals: tool_call_id unique per chat, not globally

A provider's tool-call ids are only unique within one response; two chats
(or a replayed/scripted model) can legitimately produce the same id. The
global unique index made the second chat's approval row fail to insert and
the whole round error out. Scope uniqueness to (chat_id, tool_call_id),
which is also the key the approve endpoint looks rows up by.

Revision ID: a1p2r3v4s5c6
Revises: t1a2p3r4v5l6
"""
from alembic import op

revision = "a1p2r3v4s5c6"
down_revision = "t1a2p3r4v5l6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_pending_tool_approvals_tool_call_id", table_name="pending_tool_approvals")
    op.create_index(
        "ix_pending_tool_approvals_tool_call_id", "pending_tool_approvals", ["tool_call_id"]
    )
    op.create_unique_constraint(
        "uq_pending_tool_approvals_chat_tool_call",
        "pending_tool_approvals",
        ["chat_id", "tool_call_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_pending_tool_approvals_chat_tool_call", "pending_tool_approvals", type_="unique"
    )
    op.drop_index("ix_pending_tool_approvals_tool_call_id", table_name="pending_tool_approvals")
    # Since upgrade, two chats may hold rows with the same tool_call_id, which
    # the global unique index below would refuse. Keep the newest of each id
    # and drop the others: pending approvals are transient (a round waiting
    # for a yes/no), and the older chats' waiting rounds are what a rollback
    # to global uniqueness cannot represent.
    op.execute(
        """
        DELETE FROM pending_tool_approvals a
        USING pending_tool_approvals b
        WHERE a.tool_call_id = b.tool_call_id
          AND (a.created_at, a.id::text) < (b.created_at, b.id::text)
        """
    )
    op.create_index(
        "ix_pending_tool_approvals_tool_call_id",
        "pending_tool_approvals",
        ["tool_call_id"],
        unique=True,
    )
