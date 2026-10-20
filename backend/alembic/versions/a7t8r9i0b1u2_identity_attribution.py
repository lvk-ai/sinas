"""identity attribution: identity_providers.claim_mappings, chats.attribution, llm_usage.customer

claim_mappings: an identity provider's claim -> users.custom_fields mappings,
copied at every SSO login. chats.attribution: the attribution snapshot a chat
takes when it is created. customer: the attribution value an LLM call was
for (USAGE_CUSTOMER_FIELD).

All are metadata-only on Postgres 11+: a nullable column without a default,
and a NOT NULL column with a constant default, are added without rewriting
the table, so llm_usage (one row per LLM call, large) and chats stay
writable. The
customer index is built CONCURRENTLY in its own revision (a8t9r0i1b2u3), like
c2u3s4i5d6x7's.

Revision ID: a7t8r9i0b1u2
Revises: b4f5i6l7l8b9
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op

revision = "a7t8r9i0b1u2"
down_revision = "b4f5i6l7l8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "identity_providers",
        sa.Column("claim_mappings", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column("chats", sa.Column("attribution", sa.JSON(), nullable=True))
    op.add_column("llm_usage", sa.Column("customer", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_usage", "customer")
    op.drop_column("chats", "attribution")
    op.drop_column("identity_providers", "claim_mappings")
