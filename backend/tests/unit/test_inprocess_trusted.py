"""Unit tests for the inprocess trusted executor's code-running core.

`_run_handler` is the contract-bearing piece: entry-point resolution, timeout
enforcement, input() unavailability, and JSON-result parity with the
in-container executor's shared mode. The DB-touching `execute()` wrapper is
exercised by integration tests.
"""

import pytest

from app.services.executor.base import ResultStatus
from app.services.executor.inprocess_trusted import (
    InProcessTrustedExecutor,
    _run_handler,
)


def _run(code: str, *, input_data=None, timeout: int = 5, name: str = "fn"):
    return _run_handler(
        function_code=code,
        function_namespace="test",
        function_name=name,
        input_data=input_data or {},
        context={"user_id": "u1", "execution_id": "e1"},
        timeout=timeout,
    )


def test_handler_entry_point_completes():
    r = _run("def handler(input_data, context):\n    return {'ok': input_data['x'] + 1}", input_data={"x": 1})
    assert r.status is ResultStatus.COMPLETED
    assert r.result == {"ok": 2}
    assert r.duration_ms >= 0


def test_legacy_function_name_entry_point():
    r = _run("def fn(input_data, context):\n    return 'legacy'", name="fn")
    assert r.status is ResultStatus.COMPLETED
    assert r.result == "legacy"


def test_no_entry_point_fails():
    r = _run("x = 1")
    assert r.status is ResultStatus.FAILED
    assert "handler" in r.error


def test_exception_carries_traceback():
    r = _run("def handler(input_data, context):\n    raise ValueError('boom')")
    assert r.status is ResultStatus.FAILED
    assert r.error == "boom"
    assert "ValueError" in r.traceback


def test_syntax_error_fails_at_load():
    r = _run("def handler(:")
    assert r.status is ResultStatus.FAILED
    assert "Failed to load function code" in r.error


def test_timeout_enforced():
    r = _run(
        "import time\ndef handler(input_data, context):\n    time.sleep(30)",
        timeout=1,
    )
    assert r.status is ResultStatus.FAILED
    assert "timed out" in r.error


def test_input_raises_cleanly():
    r = _run("def handler(input_data, context):\n    return input('prompt')")
    assert r.status is ResultStatus.FAILED
    assert "input() is not available" in r.error


def test_context_passed_through():
    r = _run("def handler(input_data, context):\n    return context['user_id']")
    assert r.status is ResultStatus.COMPLETED
    assert r.result == "u1"


def test_non_json_serializable_result_fails():
    r = _run("def handler(input_data, context):\n    return object()")
    assert r.status is ResultStatus.FAILED
    assert "non-JSON-serializable" in r.error


@pytest.mark.asyncio
async def test_resume_always_fails():
    r = await InProcessTrustedExecutor().resume(
        handle="h", resume_value="v", execution_id="e1", timeout=5
    )
    assert r.status is ResultStatus.FAILED
    assert "does not support resume" in r.error
