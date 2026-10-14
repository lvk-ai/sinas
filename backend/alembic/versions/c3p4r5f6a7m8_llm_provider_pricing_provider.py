"""llm_providers.pricing_provider: the LiteLLM provider family to price as

A provider of type openai pointed at another OpenAI-compatible API (Mistral,
Groq, a LiteLLM proxy) was priced as OpenAI. pricing_provider names the
family its models are looked up under instead (docs/design/usage-cost.md).
NULL keeps the provider type's own family, so existing providers price as
before.

Revision ID: c3p4r5f6a7m8
Revises: c2u3s4i5d6x7
Create Date: 2026-10-14
"""
import sqlalchemy as sa
from alembic import op

revision = "c3p4r5f6a7m8"
down_revision = "c2u3s4i5d6x7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_providers", sa.Column("pricing_provider", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_providers", "pricing_provider")
