"""Function-queue retry and dead-letter semantics (issue #135).

arq only re-runs a job that raises `Retry`, so the old "re-raise for arq
retry" never retried anything, and the DLQ was only reached when shared-pool
deferrals had pushed arq's job_try past QUEUE_MAX_RETRIES. The policy now:

- the function's own failure is final, unless the function opts in
  (`Function.max_retries`);
- the infrastructure failing before the code ran (`ExecutorUnavailable`)
  retries with backoff up to QUEUE_MAX_RETRIES, then dead-letters;
- saturation deferrals never count as failed attempts;
- a dead-lettered job can be listed and requeued through /queue/dlq.

These tests drive the real worker job and the real engine; only the executor
(and Redis, for the worker) is faked.
"""

import contextlib
import json
import uuid
from datetime import datetime

import pytest
from arq.worker import Retry
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Function
from app.models.execution import Execution, ExecutionStatus
from app.services.execution_engine import (
    ExecutionRetryScheduled,
    FunctionExecutionError,
    executor,
)
from app.services.executor.base import ExecutionResult, ExecutorUnavailable
from app.services.queue_service import (
    DLQ_KEY,
    JOB_DONE_CHANNEL_PREFIX,
    JOB_FAILURES_PREFIX,
    JOB_STATUS_PREFIX,
)
from app.services.shared_admission import SharedPoolSaturated
from tests.conftest import auth_headers


class _FakeRedis:
    """The subset of redis.asyncio the function job uses."""

    def __init__(self):
        self.store: dict = {}
        self.lists: dict = {}
        self.published: list = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)

    async def publish(self, channel, payload):
        self.published.append((channel, payload))

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)


class _FakeSandbox:
    """A sandbox executor whose every run has the given outcome."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    async def execute(self, **kwargs):
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture
def use_test_db(db, monkeypatch):
    """Engine and worker helpers open their own sessions; give them the
    test's (rolled back at the end)."""

    @contextlib.asynccontextmanager
    async def fake_session():
        yield db

    monkeypatch.setattr("app.services.execution_engine.AsyncSessionLocal", fake_session)
    monkeypatch.setattr("app.core.database.AsyncSessionLocal", fake_session)


@pytest.fixture
def sandbox(monkeypatch):
    def install(outcome):
        fake = _FakeSandbox(outcome)
        monkeypatch.setattr(executor, "_sandbox_executor", fake)
        monkeypatch.setattr(executor, "_sandbox_executor_resolved", True)
        return fake

    return install


async def _function(db: AsyncSession, user, max_retries: int = 0, input_schema=None) -> Function:
    fn = Function(
        user_id=user.id,
        namespace="default",
        name=f"retry_{uuid.uuid4().hex[:8]}",
        code="def handler(input, context): return 1",
        input_schema=input_schema or {},
        output_schema={},
        is_active=True,
        shared_pool=False,
        max_retries=max_retries,
    )
    db.add(fn)
    await db.commit()
    return fn


async def _pending_row(db: AsyncSession, user, fn: Function) -> Execution:
    row = Execution(
        user_id=user.id,
        execution_id=str(uuid.uuid4()),
        function_name=fn.name,
        trigger_type="API",
        trigger_id="test",
        status=ExecutionStatus.PENDING,
        input_data={},
        started_at=datetime.utcnow(),
    )
    db.add(row)
    await db.commit()
    return row


def _kwargs(fn: Function, row: Execution, user, input_data=None) -> dict:
    return {
        "job_id": row.execution_id,
        "function_namespace": fn.namespace,
        "function_name": fn.name,
        "input_data": input_data or {},
        "execution_id": row.execution_id,
        "trigger_type": "API",
        "trigger_id": "test",
        "user_id": str(user.id),
    }


async def _run_until_final(worker, redis, kwargs, max_runs=20):
    """Run the job the way arq does: again after every Retry, job_try + 1.
    Returns the delays asked for and the exception that ended it."""
    delays = []
    for job_try in range(1, max_runs + 1):
        try:
            await worker.execute_function_job({"redis": redis, "job_try": job_try}, **kwargs)
        except Retry as r:
            delays.append(r.defer_score / 1000)
            continue
        except Exception as e:  # noqa: BLE001 — the terminal failure
            return delays, e
        return delays, None
    raise AssertionError("job never finished")


def _dlq(redis) -> list[dict]:
    return [json.loads(raw) for raw in redis.lists.get(DLQ_KEY, [])]


class TestUserCodeFailure:
    async def test_does_not_retry_by_default(self, db, test_user, use_test_db, sandbox):
        from app.queue import worker

        fn = await _function(db, test_user)
        row = await _pending_row(db, test_user, fn)
        fake = sandbox(ExecutionResult.failed("ZeroDivisionError: division by zero"))
        redis = _FakeRedis()

        delays, exc = await _run_until_final(worker, redis, _kwargs(fn, row, test_user))

        assert delays == [] and fake.calls == 1  # ran once, never again
        assert isinstance(exc, FunctionExecutionError) and not exc.dead_letter
        await db.refresh(row)
        assert row.status == ExecutionStatus.FAILED
        assert "division by zero" in row.error and "dead-letter" not in row.error
        assert _dlq(redis) == []  # nothing to requeue: it would fail the same way
        status = json.loads(redis.store[f"{JOB_STATUS_PREFIX}{row.execution_id}"])
        assert status["status"] == "failed" and status["dead_lettered"] is False
        assert [c for c, _ in redis.published] == [f"{JOB_DONE_CHANNEL_PREFIX}{row.execution_id}"]

    async def test_retries_when_the_function_opts_in(self, db, test_user, use_test_db, sandbox):
        from app.queue import worker

        fn = await _function(db, test_user, max_retries=2)
        row = await _pending_row(db, test_user, fn)
        fake = sandbox(ExecutionResult.failed("upstream said 503"))
        redis = _FakeRedis()
        kwargs = _kwargs(fn, row, test_user)

        # First run: retry scheduled, row back to PENDING with the failure noted
        with pytest.raises(Retry):
            await worker.execute_function_job({"redis": redis, "job_try": 1}, **kwargs)
        await db.refresh(row)
        assert row.status == ExecutionStatus.PENDING
        assert row.error.startswith("Attempt 1 of 3 failed, retrying") and "503" in row.error
        assert redis.published == []  # a waiting caller is not told it failed
        status = json.loads(redis.store[f"{JOB_STATUS_PREFIX}{row.execution_id}"])
        assert status["status"] == "queued" and status["attempt"] == 1

        delays, exc = await _run_until_final(worker, redis, kwargs)
        assert fake.calls == 3 and len(delays) == 1  # 1 + 2 retries
        assert isinstance(exc, FunctionExecutionError) and exc.dead_letter and not exc.transient
        (entry,) = _dlq(redis)
        assert entry["error_type"] == "function" and entry["attempts"] == 3
        assert "503" in entry["error"]

    async def test_validation_errors_never_retry(self, db, test_user, use_test_db, sandbox):
        from app.queue import worker

        schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
        fn = await _function(db, test_user, max_retries=3, input_schema=schema)
        row = await _pending_row(db, test_user, fn)
        fake = sandbox(ExecutionResult.failed("never reached"))
        redis = _FakeRedis()

        delays, exc = await _run_until_final(worker, redis, _kwargs(fn, row, test_user))

        assert delays == [] and fake.calls == 0
        assert isinstance(exc, FunctionExecutionError) and not exc.dead_letter
        assert _dlq(redis) == []

    async def test_success_after_a_failed_attempt_clears_the_note(
        self, db, test_user, use_test_db, sandbox
    ):
        from app.queue import worker

        fn = await _function(db, test_user, max_retries=1)
        row = await _pending_row(db, test_user, fn)
        sandbox(ExecutionResult.failed("flaky"))
        redis = _FakeRedis()
        kwargs = _kwargs(fn, row, test_user)
        with pytest.raises(Retry):
            await worker.execute_function_job({"redis": redis, "job_try": 1}, **kwargs)

        sandbox(ExecutionResult.from_wire({"result": 42, "status": "completed"}))
        assert await worker.execute_function_job({"redis": redis, "job_try": 2}, **kwargs) == 42
        await db.refresh(row)
        assert (row.status, row.error, row.output_data) == (ExecutionStatus.COMPLETED, None, 42)
        assert f"{JOB_FAILURES_PREFIX}{row.execution_id}" not in redis.store


class TestInfrastructureFailure:
    async def test_retries_with_backoff_then_dead_letters(
        self, db, test_user, use_test_db, sandbox, monkeypatch
    ):
        from app.queue import worker

        monkeypatch.setattr(settings, "queue_max_retries", 3)
        monkeypatch.setattr(settings, "queue_retry_delay", 10)
        fn = await _function(db, test_user)  # no opt-in needed: the code never ran
        row = await _pending_row(db, test_user, fn)
        fake = sandbox(ExecutorUnavailable("Sandbox container failed to start: no space left"))
        redis = _FakeRedis()

        delays, exc = await _run_until_final(worker, redis, _kwargs(fn, row, test_user))

        assert fake.calls == 4  # 1 + QUEUE_MAX_RETRIES
        # 10s, 20s, 40s (+ up to 20% jitter): exponential backoff
        assert [int(d // 10) * 10 for d in delays] == [10, 20, 40]
        assert all(base <= d <= base * 1.2 for base, d in zip((10, 20, 40), delays))
        assert isinstance(exc, FunctionExecutionError) and exc.transient and exc.dead_letter

        (entry,) = _dlq(redis)
        assert entry["job_id"] == entry["execution_id"] == row.execution_id
        assert entry["error_type"] == "infrastructure" and entry["attempts"] == 4
        assert "no space left" in entry["error"]
        assert entry["function_namespace"] == fn.namespace and entry["user_id"] == str(test_user.id)

        await db.refresh(row)
        assert row.status == ExecutionStatus.FAILED
        assert "no space left" in row.error and "dead-letter queue" in row.error
        assert f"{JOB_FAILURES_PREFIX}{row.execution_id}" not in redis.store

    async def test_database_unreachable_before_the_run_retries(
        self, db, test_user, use_test_db, monkeypatch
    ):
        """A transient error while preparing the run (nothing has run yet)."""
        from app.queue import worker

        monkeypatch.setattr(settings, "queue_max_retries", 1)
        fn = await _function(db, test_user)
        row = await _pending_row(db, test_user, fn)

        async def down(*args, **kwargs):
            raise ConnectionRefusedError("connection refused")

        monkeypatch.setattr(executor, "load_function", down)
        redis = _FakeRedis()
        delays, exc = await _run_until_final(worker, redis, _kwargs(fn, row, test_user))

        assert len(delays) == 1
        assert isinstance(exc, ExecutorUnavailable)
        (entry,) = _dlq(redis)
        assert entry["error_type"] == "infrastructure" and entry["attempts"] == 2
        await db.refresh(row)
        assert row.status == ExecutionStatus.FAILED and "dead-letter" in row.error

    def test_unrelated_errors_are_not_transient(self):
        from app.services.execution_engine import is_transient_infra_error

        assert is_transient_infra_error(ConnectionRefusedError())
        assert not is_transient_infra_error(ValueError("bad input"))
        assert not is_transient_infra_error(KeyError("x"))


class TestSaturationDeferrals:
    async def test_deferrals_do_not_consume_attempts(self, test_user, monkeypatch):
        """Many deferrals, then an ordinary failure: final, but not
        dead-lettered (it used to be, once deferrals pushed job_try past
        QUEUE_MAX_RETRIES), and the engine saw no failed attempts."""
        from app.queue import worker

        seen = []
        calls = {"n": 0}

        async def engine(**kwargs):
            seen.append(kwargs["failed_attempts"])
            calls["n"] += 1
            if calls["n"] <= 12:
                raise SharedPoolSaturated("4/4 slots in use")
            err = FunctionExecutionError("Function execution failed: boom")
            err.recorded = True
            raise err

        monkeypatch.setattr(executor, "execute_function", engine)

        async def not_cancelled(execution_id):
            return False

        monkeypatch.setattr(worker, "_execution_cancelled", not_cancelled)
        redis = _FakeRedis()
        kwargs = {
            "job_id": "j1", "function_namespace": "default", "function_name": "f",
            "input_data": {}, "execution_id": str(uuid.uuid4()), "trigger_type": "API",
            "trigger_id": "t", "user_id": str(test_user.id),
        }
        delays, exc = await _run_until_final(worker, redis, kwargs, max_runs=30)

        assert len(delays) == 12 and seen == [0] * 13
        assert isinstance(exc, FunctionExecutionError)
        assert _dlq(redis) == []
        assert f"{JOB_FAILURES_PREFIX}j1" not in redis.store

    async def test_a_failure_after_deferrals_still_gets_its_retries(self, test_user, monkeypatch):
        from app.queue import worker

        monkeypatch.setattr(settings, "queue_max_retries", 2)
        seen = []

        async def engine(**kwargs):
            seen.append(kwargs["failed_attempts"])
            if len(seen) <= 5:
                raise SharedPoolSaturated("4/4 slots in use")
            n = kwargs["failed_attempts"] + 1
            if n <= settings.queue_max_retries:
                raise ExecutionRetryScheduled("pod failed", transient=True, attempt=n, max_attempts=3)
            err = FunctionExecutionError("Function execution failed: pod failed")
            err.transient = err.dead_letter = err.recorded = True
            raise err

        monkeypatch.setattr(executor, "execute_function", engine)

        async def not_cancelled(execution_id):
            return False

        monkeypatch.setattr(worker, "_execution_cancelled", not_cancelled)
        redis = _FakeRedis()
        kwargs = {
            "job_id": "j2", "function_namespace": "default", "function_name": "f",
            "input_data": {}, "execution_id": str(uuid.uuid4()), "trigger_type": "API",
            "trigger_id": "t", "user_id": str(test_user.id),
        }
        _, exc = await _run_until_final(worker, redis, kwargs, max_runs=30)

        # job_try reached 8, but the failed-attempt count only 2
        assert seen == [0, 0, 0, 0, 0, 0, 1, 2]
        (entry,) = _dlq(redis)
        assert entry["attempts"] == 3


class TestCancelledWhileWaiting:
    async def test_a_cancelled_execution_is_not_rerun(self, db, test_user, use_test_db, sandbox):
        from app.queue import worker

        fn = await _function(db, test_user, max_retries=2)
        row = await _pending_row(db, test_user, fn)
        fake = sandbox(ExecutionResult.failed("flaky"))
        redis = _FakeRedis()
        kwargs = _kwargs(fn, row, test_user)
        with pytest.raises(Retry):
            await worker.execute_function_job({"redis": redis, "job_try": 1}, **kwargs)

        row.status = ExecutionStatus.CANCELLED  # e.g. its batch was cancelled
        await db.commit()
        assert await worker.execute_function_job({"redis": redis, "job_try": 2}, **kwargs) is None
        assert fake.calls == 1
        status = json.loads(redis.store[f"{JOB_STATUS_PREFIX}{row.execution_id}"])
        assert status["status"] == "cancelled"


class TestDeadLetterQueueApi:
    async def test_list_and_requeue(self, client, db, admin_user, test_user, use_test_db, monkeypatch):
        """A dead-lettered job is listed with its error and can be requeued —
        under the same id, although arq still holds the failed run's result
        (it used to refuse that silently)."""
        from arq.connections import create_pool
        from arq.constants import result_key_prefix

        from app.core.redis import get_redis, get_redis_settings

        fn = await _function(db, test_user)
        row = await _pending_row(db, test_user, fn)
        row.status, row.error = ExecutionStatus.FAILED, "boom (moved to the dead-letter queue)"
        await db.commit()
        eid = row.execution_id

        redis = await get_redis()
        pool = await create_pool(get_redis_settings())
        monkeypatch.setattr("app.services.queue_service.get_arq_pool", lambda: _ret(pool))
        try:
            entry = {
                **_kwargs(fn, row, test_user),
                "function": f"{fn.namespace}/{fn.name}",
                "callback_url": None, "depth": 0, "chat_id": None,
                "error": "Sandbox container failed to start", "error_type": "infrastructure",
                "attempts": 4, "failed_at": 1.0,
            }
            await redis.lpush(DLQ_KEY, json.dumps(entry))
            await redis.set(f"{result_key_prefix}{eid}", b"failed-run", ex=3600)
            await redis.set(f"{JOB_FAILURES_PREFIX}{eid}", "4")
            h = auth_headers(admin_user)

            r = await client.get("/api/v1/queue/dlq", headers=h)
            assert r.status_code == 200
            (listed,) = [e for e in r.json() if e["job_id"] == eid]
            assert listed["error"] == "Sandbox container failed to start"
            assert (listed["error_type"], listed["attempts"]) == ("infrastructure", 4)

            r = await client.post(f"/api/v1/queue/dlq/{eid}/retry", headers=h)
            assert r.status_code == 200, r.text
            assert r.json()["new_job_id"] == eid

            # Really queued for arq, out of the DLQ, failure count reset
            assert await redis.zscore("sinas:queue:functions", eid) is not None
            assert all(json.loads(e)["job_id"] != eid for e in await redis.lrange(DLQ_KEY, 0, -1))
            assert await redis.get(f"{JOB_FAILURES_PREFIX}{eid}") is None
            await db.refresh(row)
            assert (row.status, row.error) == (ExecutionStatus.PENDING, None)

            # Requeueing again while it is queued is refused, not lost
            await redis.lpush(DLQ_KEY, json.dumps(entry))
            r = await client.post(f"/api/v1/queue/dlq/{eid}/retry", headers=h)
            assert r.status_code == 409
            assert len(await redis.lrange(DLQ_KEY, 0, -1)) == 1
        finally:
            await pool.aclose()


async def _ret(value):
    return value


class TestWorkerSettings:
    def test_arq_backstop_covers_deferrals_and_retries(self):
        from app.queue.worker import WorkerSettings
        from app.schemas.spec.function import MAX_FUNCTION_RETRIES

        floor = settings.queue_saturation_timeout_seconds // 30 + max(
            settings.queue_max_retries, MAX_FUNCTION_RETRIES
        )
        assert WorkerSettings.max_tries > floor
        assert not hasattr(WorkerSettings, "retry_delay")  # arq never read it


class TestFunctionSetting:
    async def test_max_retries_round_trips_and_is_bounded(self, client, admin_user):
        h = auth_headers(admin_user)
        body = {
            "namespace": "default", "name": f"mr_{uuid.uuid4().hex[:8]}",
            "code": "def handler(input, context):\n    return 1\n",
            "input_schema": {"type": "object"}, "output_schema": {"type": "object"},
        }
        r = await client.post("/api/v1/functions", json=body, headers=h)
        assert r.status_code == 201 and r.json()["max_retries"] == 0
        url = f"/api/v1/functions/default/{body['name']}"
        r = await client.put(url, json={"max_retries": 3}, headers=h)
        assert r.status_code == 200 and r.json()["max_retries"] == 3
        r = await client.put(url, json={"max_retries": 99}, headers=h)
        assert r.status_code == 422
