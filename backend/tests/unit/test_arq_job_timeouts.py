"""Job timeouts with the arq the lockfile pins.

arq's `enqueue_job` has no per-job timeout: a `_job_timeout` keyword is not
one of its parameters, so it lands in the job's kwargs (ignored by a
`**kwargs` job, a TypeError for any other) and the job runs under its
function's or the worker's timeout instead. Timeouts live on the job
definitions (`func(..., timeout=)`), and agent jobs, whose timeout is per
chat, enforce their own `job_timeout`.
"""

import ast
import asyncio
import inspect
import time
from pathlib import Path

import pytest
from arq.connections import ArqRedis
from arq.worker import Function, func

from app.core.config import settings
from app.queue import agent_jobs, worker
from app.services import queue_service as qs
from app.services.queue_service import queue_service

APP_DIR = Path(__file__).resolve().parents[2] / "app"
WORKERS = (worker.WorkerSettings, worker.AgentWorkerSettings, worker.SubAgentWorkerSettings,
           worker.PipelineWorkerSettings)
AGENT_JOBS = ("execute_agent_message_job", "execute_agent_resume_job", "execute_agent_delegate_resume_job")


def _enqueue_options() -> set[str]:
    """The `_`-keywords arq's enqueue_job takes itself; any other keyword
    goes to the job."""
    return {
        name for name, p in inspect.signature(ArqRedis.enqueue_job).parameters.items()
        if name.startswith("_") and p.kind is inspect.Parameter.KEYWORD_ONLY
    }


def _functions(settings_cls) -> dict[str, Function]:
    return {f.name: f for f in (func(x) for x in settings_cls.functions)}


def _registered(name: str) -> Function:
    for cls in WORKERS:
        if name in (fns := _functions(cls)):
            return fns[name]
    raise AssertionError(f"{name} is not registered with any worker")


def test_enqueue_options_are_what_the_locked_arq_takes():
    options = _enqueue_options()
    assert {"_job_id", "_queue_name", "_defer_by", "_defer_until", "_expires", "_job_try"} <= options
    assert "_job_timeout" not in options


def test_no_enqueue_in_the_app_passes_an_option_arq_does_not_take():
    """Every `_`-keyword handed to enqueue_job anywhere in app/, directly or
    through an `enqueue_kwargs[...]` dict, is one arq's signature has."""
    options = _enqueue_options()
    bad = []
    for path in APP_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "enqueue_job"):
                for kw in node.keywords:
                    if kw.arg and kw.arg.startswith("_") and kw.arg not in options:
                        bad.append(f"{path.relative_to(APP_DIR)}:{node.lineno} {kw.arg}")
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                            and "enqueue" in target.value.id
                            and isinstance(target.slice, ast.Constant)
                            and isinstance(target.slice.value, str)
                            and target.slice.value.startswith("_")
                            and target.slice.value not in options):
                        bad.append(f"{path.relative_to(APP_DIR)}:{node.lineno} {target.slice.value}")
    assert bad == []


class StrictPool:
    """Takes enqueues the way arq would: only its own `_`-options, and the
    rest must bind to the registered job function's signature."""

    def __init__(self):
        self.jobs: list[tuple[str, dict]] = []

    async def enqueue_job(self, function, /, *args, **kwargs):
        unknown = {k for k in kwargs if k.startswith("_")} - _enqueue_options()
        assert not unknown, f"{function}: arq's enqueue_job takes no {sorted(unknown)}"
        job_kwargs = {k: v for k, v in kwargs.items() if not k.startswith("_")}
        inspect.signature(_registered(function).coroutine).bind({}, *args, **job_kwargs)
        self.jobs.append((function, kwargs))
        return object()  # arq returns a Job (None when the id is taken)


class FakeRedis:
    async def set(self, *a, **k):
        return True


@pytest.fixture
def pool(monkeypatch):
    p = StrictPool()

    async def get_pool():
        return p

    async def get_redis():
        return FakeRedis()

    monkeypatch.setattr(qs, "get_arq_pool", get_pool)
    monkeypatch.setattr(qs, "get_redis", get_redis)
    monkeypatch.setattr("app.core.redis.get_arq_pool", get_pool)
    return p


async def test_agent_enqueues_carry_the_timeout_as_a_job_argument(pool):
    await queue_service.enqueue_agent_message(
        chat_id="c", user_id="u", user_token="t", content="hi", channel_id="ch", job_timeout=900,
    )
    await queue_service.enqueue_agent_resume(
        chat_id="c", user_id="u", user_token="t", pending_approval_id="p", approved=True,
        channel_id="ch", job_timeout=901, defer_seconds=30,
    )
    await queue_service.enqueue_agent_delegate_resume(
        chat_id="c", user_id="u", user_token="t", channel_id="ch", conversation_context={},
        job_timeout=902, defer_seconds=30,
    )
    await queue_service.enqueue_agent_message(
        chat_id="c", user_id="u", user_token="t", content="hi", channel_id="ch",
    )
    assert [(name, kw.get("job_timeout")) for name, kw in pool.jobs] == [
        ("execute_agent_message_job", 900),
        ("execute_agent_resume_job", 901),
        ("execute_agent_delegate_resume_job", 902),
        ("execute_agent_message_job", None),
    ]


async def test_background_job_enqueue_passes_only_arq_options(pool, monkeypatch):
    from app.services import chat_wakeups

    monkeypatch.setattr(settings, "code_execution_enabled", True)
    out = await chat_wakeups.start_background_job("chat-x", "user-x", "print(1)", "d")
    ((name, kw),) = pool.jobs
    assert name == "execute_background_code_job" and kw["_job_id"] == out["job_id"]


async def test_function_and_pipeline_enqueues_pass_only_arq_options(pool):
    await queue_service.enqueue_function(
        function_namespace="ns", function_name="f", input_data={}, execution_id="e",
        trigger_type="manual", trigger_id="t", user_id="u", delay=5,
    )
    await queue_service.enqueue_pipeline_run("p", {}, "manual", None, "u")
    await queue_service.enqueue_pipeline_fire("ns", "p", {}, "manual", None)
    assert [name for name, _ in pool.jobs] == [
        "execute_function_job", "execute_pipeline_run_job", "execute_pipeline_fire_job",
    ]


def test_background_job_definition_carries_its_timeout():
    fn = _functions(worker.WorkerSettings)["execute_background_code_job"]
    assert fn.timeout_s == settings.code_execution_timeout + settings.background_job_grace_seconds


@pytest.mark.parametrize("settings_cls", [worker.AgentWorkerSettings, worker.SubAgentWorkerSettings])
def test_agent_job_definitions_leave_room_for_the_longest_job_timeout(settings_cls):
    fns = _functions(settings_cls)
    for name in AGENT_JOBS:
        assert fns[name].timeout_s == agent_jobs.agent_function_timeout(), name
        assert fns[name].timeout_s > agent_jobs.agent_job_timeout_ceiling() >= settings.agent_job_timeout


@pytest.mark.parametrize("name", AGENT_JOBS)
def test_every_agent_job_enforces_its_own_timeout(name):
    job = getattr(agent_jobs, name)
    chain = []
    while job is not None:
        chain.append(job.__code__.co_qualname)
        job = getattr(job, "__wrapped__", None)
    assert "_within_job_timeout.<locals>.run" in chain


def test_timeout_resolution(monkeypatch):
    monkeypatch.setattr(settings, "agent_job_timeout", 600)
    monkeypatch.setattr(settings, "agent_job_timeout_max", 3600)
    assert agent_jobs.agent_job_timeout(None) == 600
    assert agent_jobs.agent_job_timeout(0) == 600
    assert agent_jobs.agent_job_timeout(120) == 120
    assert agent_jobs.agent_job_timeout(1800) == 1800
    assert agent_jobs.agent_job_timeout(86400) == 3600
    # A max below the default never shortens the default.
    monkeypatch.setattr(settings, "agent_job_timeout_max", 60)
    assert agent_jobs.agent_job_timeout(None) == 600


class TestEnforcement:
    @staticmethod
    def _sleeper(record):
        @agent_jobs._within_job_timeout
        async def job(ctx, **kwargs):
            try:
                await asyncio.sleep(5)
            finally:
                record.append("cleanup")
        return job

    async def test_the_jobs_own_timeout_cancels_it(self):
        record = []
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await self._sleeper(record)({}, job_timeout=0.1)
        assert time.monotonic() - start < 2
        assert record == ["cleanup"]

    async def test_without_one_the_default_applies(self, monkeypatch):
        monkeypatch.setattr(settings, "agent_job_timeout", 0.1)
        with pytest.raises(TimeoutError):
            await self._sleeper([])({})

    async def test_a_longer_one_is_capped(self, monkeypatch):
        monkeypatch.setattr(settings, "agent_job_timeout", 0.05)
        monkeypatch.setattr(settings, "agent_job_timeout_max", 0.1)
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await self._sleeper([])({}, job_timeout=3600)
        assert time.monotonic() - start < 2

    async def test_a_job_within_its_timeout_runs_to_the_end(self):
        @agent_jobs._within_job_timeout
        async def job(ctx, **kwargs):
            await asyncio.sleep(0.01)
            return kwargs["job_timeout"]

        assert await job({}, job_timeout=5) == 5
