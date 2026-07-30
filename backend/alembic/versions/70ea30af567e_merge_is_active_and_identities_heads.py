"""merge is_active and identities heads

Revision ID: 70ea30af567e
Revises: m1e2r3g4e5i6, u1s2r3a4c5t6
Create Date: 2026-07-30 10:56:39.255245

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '70ea30af567e'
down_revision = ('m1e2r3g4e5i6', 'u1s2r3a4c5t6')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass