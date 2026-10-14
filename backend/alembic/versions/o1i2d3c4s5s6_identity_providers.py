"""identity_providers + user_roles.source + refresh_tokens.identity_provider

SSO login via OpenID Connect (docs/design/oidc-sso.md): identity providers
as a resource kind; memberships an IdP's role mapping granted are tagged
(user_roles.source) so a later login can end them without touching ones
given by hand; refresh tokens record the provider whose login issued them.
Additive.

Revision ID: o1i2d3c4s5s6
Revises: r1e2p3o4s5i6
Create Date: 2026-10-14
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "o1i2d3c4s5s6"
down_revision = "r1e2p3o4s5i6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "identity_providers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("issuer", sa.String(2048), nullable=False),
        sa.Column("client_id", sa.String(512), nullable=False),
        sa.Column("client_secret", sa.String(255), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("email_claim", sa.String(512), nullable=False, server_default="email"),
        sa.Column("groups_claim", sa.String(512), nullable=False, server_default="groups"),
        sa.Column("role_mappings", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("allowed_domains", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("enforce_for_domains", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("auto_provision", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("default_role", sa.String(255), nullable=True),
        sa.Column("session_hours", sa.Integer(), nullable=False, server_default="12"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("managed_by", sa.Text(), nullable=True),
        sa.Column("config_name", sa.Text(), nullable=True),
        sa.Column("config_checksum", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_identity_providers_name", "identity_providers", ["name"], unique=True)
    op.add_column("user_roles", sa.Column("source", sa.String(255), nullable=True))
    op.add_column("refresh_tokens", sa.Column("identity_provider", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("refresh_tokens", "identity_provider")
    op.drop_column("user_roles", "source")
    op.drop_index("ix_identity_providers_name", table_name="identity_providers")
    op.drop_table("identity_providers")
