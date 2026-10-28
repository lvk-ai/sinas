"""llm_usage index for the usage API's customer dimension: (customer, created_at)

Built CONCURRENTLY, like c2u3s4i5d6x7's, so the insert every LLM call makes
is not blocked while it builds; in a revision of its own because CONCURRENTLY
runs outside a transaction. A failed build leaves an INVALID index behind, so
it is dropped first if present: rerunning this revision is always safe.

Revision ID: a8t9r0i1b2u3
Revises: a7t8r9i0b1u2
Create Date: 2026-10-20
"""
from alembic import op

revision = "a8t9r0i1b2u3"
down_revision = "a7t8r9i0b1u2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_llm_usage_customer_created")
        op.execute(
            "CREATE INDEX CONCURRENTLY idx_llm_usage_customer_created "
            "ON llm_usage (customer, created_at)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_llm_usage_customer_created")
