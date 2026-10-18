"""package_requires: packages record what they require and how they came

docs/design/package-dependencies.md. A package declares the packages it
needs (package.requires); the installed version's requirements are kept on
its record so dependents are found from this table (uninstall refuses while
any exist), and installed_as_dependency marks packages a dependency plan
installed rather than the operator directly (so orphans can be shown).

Existing rows: [] and false. Nothing installed before this declared
requirements Sinas understood.

Revision ID: p7k8g9d0e1p2
Revises: c4h5a6t7p8g9
Create Date: 2026-10-18
"""

import sqlalchemy as sa

from alembic import op

revision = "p7k8g9d0e1p2"
down_revision = "c4h5a6t7p8g9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "packages",
        sa.Column("requires", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "packages",
        sa.Column(
            "installed_as_dependency", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("packages", "installed_as_dependency")
    op.drop_column("packages", "requires")
