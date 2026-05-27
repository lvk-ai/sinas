"""add batches table and link from executions

Revision ID: b1a2t3c4h5e6
Revises: c1a2l3b4k5s6
Create Date: 2026-05-14
"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "b1a2t3c4h5e6"
down_revision = "c1a2l3b4k5s6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "batches",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("kind", sa.Enum("FUNCTION", "AGENT", name="batchkind"), nullable=False),
        sa.Column("target_namespace", sa.String(255), nullable=False),
        sa.Column("target_name", sa.String(255), nullable=False),
        sa.Column("total", sa.Integer, nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED", "RUNNING", "COMPLETED", "PARTIAL", "FAILED", "CANCELLED",
                name="batchstatus",
            ),
            nullable=False,
            index=True,
            server_default="QUEUED",
        ),
        sa.Column("trigger_id_prefix", sa.String(255), nullable=True),
        sa.Column("callback_url", sa.Text, nullable=True),
        sa.Column("batch_callback_url", sa.Text, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("callback_status", sa.String(20), nullable=True),
        sa.Column("callback_response_code", sa.Integer, nullable=True),
    )
    op.create_index("ix_batches_user_status", "batches", ["user_id", "status"])

    op.add_column(
        "executions",
        sa.Column("batch_id", sa.UUID(as_uuid=True), sa.ForeignKey("batches.id"), nullable=True),
    )
    op.create_index("ix_executions_batch_id", "executions", ["batch_id"])


def downgrade() -> None:
    op.drop_index("ix_executions_batch_id", table_name="executions")
    op.drop_column("executions", "batch_id")
    op.drop_index("ix_batches_user_status", table_name="batches")
    op.drop_table("batches")
    op.execute("DROP TYPE IF EXISTS batchstatus")
    op.execute("DROP TYPE IF EXISTS batchkind")
