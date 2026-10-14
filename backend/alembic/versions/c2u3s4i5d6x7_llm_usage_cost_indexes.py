"""llm_usage indexes for the usage API: (agent, created_at), (provider_name, created_at)

The usage API filters a time range by agent or provider (user and chat
already have their indexes). Built CONCURRENTLY, like ix_messages_created_at,
so the insert every LLM call makes is not blocked while they build.

A revision of its own: CONCURRENTLY runs outside a transaction, so had it
shared one with c1o2s3t4u5s6, a failed build would leave that revision's
columns committed but unrecorded, and a rerun would fail on them. A build
that fails leaves an INVALID index behind, so each is dropped first if
present: rerunning this revision is always safe.

Revision ID: c2u3s4i5d6x7
Revises: c1o2s3t4u5s6
Create Date: 2026-10-14
"""
from alembic import op

revision = "c2u3s4i5d6x7"
down_revision = "c1o2s3t4u5s6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_llm_usage_agent_created")
        op.execute(
            "CREATE INDEX CONCURRENTLY idx_llm_usage_agent_created "
            "ON llm_usage (agent, created_at)"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_llm_usage_provider_created")
        op.execute(
            "CREATE INDEX CONCURRENTLY idx_llm_usage_provider_created "
            "ON llm_usage (provider_name, created_at)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_llm_usage_provider_created")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_llm_usage_agent_created")
