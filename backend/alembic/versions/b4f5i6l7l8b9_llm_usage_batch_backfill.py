"""llm_usage_batch_backfill: mark the batch poller's existing rows batch = true, in short batches

c1o2s3t4u5s6 added llm_usage.batch (default false). Rows the provider batch
poller wrote before it (source = 'provider_batch') are batch calls, so they
are marked here.

c1o2s3t4u5s6 in 0.5.0-rc.1 did this with one UPDATE in the same transaction
as its ADD COLUMNs, holding ACCESS EXCLUSIVE on llm_usage until every such
row was rewritten: 18 s on 800k rows, during which every LLM call's usage
insert waited. Here each statement updates at most BATCH_ROWS rows of one
primary-key range and commits on its own (autocommit), so it only takes
row locks, briefly, and llm_usage stays writable throughout.

Idempotent: only rows still batch = false are touched, so a database that
already ran rc.1's c1o2s3t4u5s6 (which marked them all) updates nothing,
and a rerun after an interrupted upgrade carries on where it stopped.

Rewriting the rows leaves dead tuples behind; run VACUUM (ANALYZE)
llm_usage after upgrading (autovacuum gets there too, eventually).

Revision ID: b4f5i6l7l8b9
Revises: r8e9t0i1d2x3
Create Date: 2026-10-19
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "b4f5i6l7l8b9"
down_revision = "r8e9t0i1d2x3"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

BATCH_ROWS = 50_000

_MARK = (
    "UPDATE llm_usage SET batch = true "
    "WHERE batch = false AND source = 'provider_batch'"
)


def upgrade() -> None:
    if op.get_context().as_sql:
        # Offline (--sql): no rows to page through; emit the plain statement.
        op.execute(_MARK)
        return

    marked = 0
    lower = None
    with op.get_context().autocommit_block():
        conn = op.get_bind()  # in autocommit: each statement commits itself
        while True:
            # The id that closes the next range of BATCH_ROWS rows (in primary
            # key order), or None when fewer than that are left.
            upper = conn.execute(
                sa.text(
                    "SELECT id FROM llm_usage"
                    + (" WHERE id > :lower" if lower is not None else "")
                    + " ORDER BY id OFFSET :n LIMIT 1"
                ),
                {"lower": lower, "n": BATCH_ROWS - 1},
            ).scalar()

            where = []
            if lower is not None:
                where.append("id > :lower")
            if upper is not None:
                where.append("id <= :upper")
            sql = _MARK + "".join(f" AND {w}" for w in where)
            marked += conn.execute(sa.text(sql), {"lower": lower, "upper": upper}).rowcount

            if upper is None:
                break
            lower = upper

    log.info("llm_usage: marked %d provider batch row(s) batch = true", marked)


def downgrade() -> None:
    # Nothing to undo: c1o2s3t4u5s6's downgrade drops the column.
    pass
