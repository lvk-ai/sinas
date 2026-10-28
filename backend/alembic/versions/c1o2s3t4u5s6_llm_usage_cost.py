"""llm_usage cost: cost_usd/price_source/batch, provider pricing, ratecard cache

LLM cost tracking (docs/design/usage-cost.md). Each llm_usage row gets the
cost it was priced at when written (NULL when no price was known), where
that price came from, and whether it was a provider batch-API call. LLM
providers get their own pricing, model aliases and a discount multiplier.
ratecard_cache keeps the last good copy of a ratecard fetched from
MODEL_PRICES_URL.

Existing rows stay unpriced (POST /usage/backfill prices them). Rows the
batch poller wrote are marked batch by b4f5i6l7l8b9, in short batches: an
UPDATE of every such row here held ACCESS EXCLUSIVE on llm_usage for as
long as it took to rewrite them (18 s on 800k rows), as this revision's
ADD COLUMNs take that lock.

All in one transaction, so a failure leaves nothing behind. The indexes the
usage API needs are built CONCURRENTLY (outside a transaction) by the next
revision, c2u3s4i5d6x7: kept apart so a failed build can simply be rerun.

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


def downgrade() -> None:
    op.drop_table("ratecard_cache")
    op.drop_column("llm_providers", "pricing_discount")
    op.drop_column("llm_providers", "model_aliases")
    op.drop_column("llm_providers", "pricing")
    op.drop_column("llm_usage", "batch")
    op.drop_column("llm_usage", "price_source")
    op.drop_column("llm_usage", "cost_usd")
