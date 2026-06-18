"""Unit tests for the executor contract's wire translation.

ExecutionResult.from_wire is the single boundary where the in-container
executor's JSON dict becomes the typed result the engine consumes, so it must
map every shape the container (and the poll scripts) can emit. See
app/services/executor/base.py and ADR 2026-06-17-ephemeral-sandbox-baked-image.
"""

from app.services.executor.base import ExecutionResult, ResultStatus


def test_completed_explicit_status():
    r = ExecutionResult.from_wire({"result": {"x": 1}, "duration_ms": 42, "status": "completed"})
    assert r.status is ResultStatus.COMPLETED
    assert r.result == {"x": 1}
    assert r.duration_ms == 42
    assert r.error is None


def test_completed_without_status():
    # Success envelopes don't always carry an explicit status.
    r = ExecutionResult.from_wire({"result": 7, "duration_ms": 3})
    assert r.status is ResultStatus.COMPLETED
    assert r.result == 7


def test_failed_with_traceback():
    r = ExecutionResult.from_wire({"status": "failed", "error": "boom", "traceback": "TB"})
    assert r.status is ResultStatus.FAILED
    assert r.error == "boom"
    assert r.traceback == "TB"


def test_failed_without_error_field_defaults_message():
    r = ExecutionResult.from_wire({"status": "failed"})
    assert r.status is ResultStatus.FAILED
    assert r.error == "Unknown error"


def test_status_less_error_envelope_maps_to_failed():
    # The poll scripts emit {"error": "...timeout"} with no status — this must
    # never be misread as a successful (result=None) completion.
    r = ExecutionResult.from_wire({"error": "Execution timeout after 300s"})
    assert r.status is ResultStatus.FAILED
    assert r.error == "Execution timeout after 300s"


def test_awaiting_input_maps_container_name_to_handle():
    r = ExecutionResult.from_wire(
        {"status": "awaiting_input", "prompt": "continue?", "container_name": "sinas-shared-1"}
    )
    assert r.status is ResultStatus.AWAITING_INPUT
    assert r.prompt == "continue?"
    assert r.handle == "sinas-shared-1"


def test_awaiting_without_handle():
    # A subsequent input() may omit container_name; the resume helper re-stamps it.
    r = ExecutionResult.from_wire({"status": "awaiting_input", "prompt": "again?"})
    assert r.status is ResultStatus.AWAITING_INPUT
    assert r.handle is None


def test_failed_constructor():
    r = ExecutionResult.failed("nope")
    assert r.status is ResultStatus.FAILED
    assert r.error == "nope"
    assert r.traceback is None
