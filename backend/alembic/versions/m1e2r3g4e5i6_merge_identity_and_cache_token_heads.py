"""merge identity and cache-token heads

Revision ID: m1e2r3g4e5i6
Revises: c1a2c3h4e5t6, u1i2d3e4n5t6
Create Date: 2026-07-30 10:41:30.167884

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'm1e2r3g4e5i6'
down_revision = ('c1a2c3h4e5t6', 'u1i2d3e4n5t6')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass