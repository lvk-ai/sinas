"""llm_usage cost: cost_usd/price_source/batch, provider pricing, ratecard cache

LLM cost tracking (docs/design/usage-cost.md). Each llm_usage row gets the
cost it was priced at when written (NULL when no price was known), where
that price came from, and whether it was a provider batch-API call. LLM
providers get their own pricing, model aliases and a discount multiplier.
ratecard_cache keeps the last good copy of a ratecard fetched from
MODEL_PRICES_URL.

Existing rows stay unpriced (POST /usage/backfill prices them); rows the
batch poller wrote are marked batch.

The usage API filters a time range by agent or provider: both get an index
led by that column, built CONCURRENTLY like ix_messages_created_at so the
inserts every LLM call makes are not blocked while they build. A build that
fails leaves an INVALID index behind, so each is dropped first if present.

Revision ID: c1o2s3t4u5s6
Revises: o1i2d3c4s5s6
Create Date: 2026-10-14
"""
import sqlalchemy as sa
from alembic import op

revision = "c1o2s3t4u5s6"
down_revision = "o1i2d3c4s5s6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_usage", sa.Column("cost_usd", sa.Numeric(20, 10), nullable=True))
    op.add_column("llm_usage", sa.Column("price_source", sa.String(20), nullable=True))
    op.add_column(
        "llm_usage", sa.Column("batch", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.execute("UPDATE llm_usage SET batch = true WHERE source = 'provider_batch'")

    op.add_column("llm_providers", sa.Column("pricing", sa.JSON(), nullable=True))
    op.add_column("llm_providers", sa.Column("model_aliases", sa.JSON(), nullable=True))
    op.add_column("llm_providers", sa.Column("pricing_discount", sa.Float(), nullable=True))

    op.create_table(
        "ratecard_cache",
        sa.Column("source", sa.String(20), primary_key=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("entries", sa.JSON(), nullable=True),
        sa.Column("model_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
    )

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
    op.drop_table("ratecard_cache")
    op.drop_column("llm_providers", "pricing_discount")
    op.drop_column("llm_providers", "model_aliases")
    op.drop_column("llm_providers", "pricing")
    op.drop_column("llm_usage", "batch")
    op.drop_column("llm_usage", "price_source")
    op.drop_column("llm_usage", "cost_usd")
