"""Migration b4f5i6l7l8b9: mark the provider batch poller's old llm_usage rows
batch = true, one primary-key range per committed UPDATE.

Each test runs the revision's upgrade() in a database of its own, created on
the TEST_DATABASE_URL server and dropped afterwards: the revision commits as
it goes, so it must never run against a database that holds real rows. There
is no fallback: without TEST_DATABASE_URL these tests fail.
"""

import importlib.util
import io
import os
import uuid
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
B = 3  # BATCH_ROWS for these tests


def _load(name):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def backfill(monkeypatch):
    module = _load("b4f5i6l7l8b9_llm_usage_batch_backfill")
    monkeypatch.setattr(module, "BATCH_ROWS", B)
    return module


@pytest.fixture
def engine():
    """A sync engine on a throwaway database with just the columns the
    revision reads and writes."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL is not set: refusing to run a committing migration test")
    server = make_url(url).set(drivername="postgresql+psycopg2")
    name = f"t_backfill_{uuid.uuid4().hex[:12]}"
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    eng = create_engine(server.set(database=name))
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE llm_usage (id uuid PRIMARY KEY, source varchar(50), "
                "batch boolean NOT NULL DEFAULT false)"
            )
        )
    try:
        yield eng
    finally:
        eng.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _insert(engine, rows):
    """rows: [(source, batch)], in a random id order. Returns the ids, sorted."""
    ids = []
    with engine.begin() as conn:
        for source, batch in rows:
            i = uuid.uuid4()
            ids.append(i)
            conn.execute(
                text("INSERT INTO llm_usage (id, source, batch) VALUES (:i, :s, :b)"),
                {"i": i, "s": source, "b": batch},
            )
    return sorted(ids)


def _state(engine):
    with engine.connect() as conn:
        return {
            r.id: (r.source, r.batch)
            for r in conn.execute(text("SELECT id, source, batch FROM llm_usage"))
        }


def _run(engine, backfill, fail_on_update=None):
    """Run upgrade() as alembic would; return each UPDATE's row count.
    fail_on_update=n raises before the n-th UPDATE (1-based) is sent."""
    counts = []
    with engine.connect() as conn:

        @event.listens_for(conn, "before_cursor_execute")
        def before(conn_, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE llm_usage") and fail_on_update == len(counts) + 1:
                raise RuntimeError("interrupted")

        @event.listens_for(conn, "after_cursor_execute")
        def after(conn_, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE llm_usage"):
                counts.append(cursor.rowcount)

        with Operations.context(MigrationContext.configure(conn)):
            backfill.upgrade()
    return counts


def _expected_after(state):
    return {
        i: (source, True if source == "provider_batch" else batch)
        for i, (source, batch) in state.items()
    }


def test_revision_chain_and_the_update_moved_out_of_c1o2s3t4u5s6(backfill):
    assert backfill.down_revision == "r8e9t0i1d2x3"
    c1 = (VERSIONS / "c1o2s3t4u5s6_llm_usage_cost.py").read_text()
    assert "UPDATE" not in c1.split("def upgrade")[1].split("def downgrade")[0]


def test_empty_table(engine, backfill):
    assert _run(engine, backfill) == [0]  # one UPDATE over the (empty) tail
    assert _state(engine) == {}


def test_exactly_one_batch(engine, backfill):
    _insert(engine, [("provider_batch", False)] * B)
    # A full range, then the empty tail after it.
    assert _run(engine, backfill) == [B, 0]
    assert all(batch for _, batch in _state(engine).values())


def test_several_batches_mark_only_unmarked_provider_batch_rows(engine, backfill):
    rows = [("provider_batch", False)] * 5 + [("chat", False)] * 2 + [(None, False)] + [("provider_batch", True)]
    _insert(engine, rows)
    before = _state(engine)

    counts = _run(engine, backfill)

    assert len(counts) == len(rows) // B + 1  # 9 rows: three ranges and the empty tail
    assert all(n <= B for n in counts)
    assert sum(counts) == 5
    assert _state(engine) == _expected_after(before)


def test_each_range_is_committed_on_its_own_and_a_restart_resumes(engine, backfill):
    ids = _insert(engine, [("provider_batch", False)] * 7)

    with pytest.raises(RuntimeError, match="interrupted"):
        _run(engine, backfill, fail_on_update=2)

    # The first range (the B lowest ids) stayed committed; nothing after it ran.
    state = _state(engine)
    assert [state[i][1] for i in ids] == [True] * B + [False] * (len(ids) - B)

    # Rerun: the first range has nothing left, the rest get marked.
    counts = _run(engine, backfill)
    assert counts == [0, B, 1]
    assert all(batch for _, batch in _state(engine).values())


def test_already_marked_rows_are_not_rewritten(engine, backfill):
    """Rows rc.1's c1o2s3t4u5s6 already marked (and every other row the
    filter excludes) keep their tuple: no dead rows, no row locks."""
    _insert(engine, [("provider_batch", True)] * 4 + [("chat", False)] * 2 + [("provider_batch", False)])

    def xmins():
        with engine.connect() as conn:
            return dict(conn.execute(text("SELECT id, xmin::text FROM llm_usage")).all())

    before_state, before_xmin = _state(engine), xmins()
    assert sum(_run(engine, backfill)) == 1
    after_xmin = xmins()
    changed = {i for i in before_xmin if before_xmin[i] != after_xmin[i]}
    assert changed == {i for i, (s, b) in before_state.items() if s == "provider_batch" and not b}

    # And a second run, as on a database that ran rc.1, changes nothing.
    assert sum(_run(engine, backfill)) == 0
    assert xmins() == after_xmin


def test_offline_mode_emits_the_plain_update(backfill):
    buf = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buf}
    )
    with Operations.context(context):
        backfill.upgrade()
    sql = buf.getvalue()
    assert "UPDATE llm_usage SET batch = true WHERE batch = false AND source = 'provider_batch'" in sql
    assert "OFFSET" not in sql  # no paging without a database to page through
