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


# ---------------------------------------------------------------------------
# Code-execution result shaping: the in-container executor reports the
# wrapper's return value under "result"; _shape_code_result must lift the
# run's own stdout / status / error out of it.
# ---------------------------------------------------------------------------


def _run_wrapped(code: str, workbench_input: dict | None = None) -> dict:
    """Run the real wrapper in-process and shape it as the executor reports it
    (container_executor._execute_inline_sandbox)."""
    from app.services.code_execution import _build_wrapper, _shape_code_result

    ns: dict = {}
    exec(_build_wrapper(code, workbench=workbench_input is not None), ns)
    output = ns["handler"](workbench_input or {}, {"execution_id": "e"})
    return _shape_code_result({"result": output, "execution_id": "e", "status": "completed"}, 5)


def test_code_result_lifts_stdout_and_value():
    out = _run_wrapped("print('hi')\n41 + 1")
    assert out == {"stdout": "hi\n", "stderr": "", "result": 42, "duration_ms": 5}


def test_code_that_raises_is_an_error_at_the_top_level():
    out = _run_wrapped("print('before')\nraise ValueError('boom')")
    assert out["stdout"] == "before\n"
    assert out["result"] is None
    assert "ValueError: boom" in out["error"]


def test_a_returned_dict_shaped_like_the_wrapper_output_stays_the_value():
    out = _run_wrapped("{'status': 'completed', 'stdout': 'x', 'result': 1}")
    assert out["result"] == {"status": "completed", "stdout": "x", "result": 1}
    assert out["stdout"] == ""


def test_workbench_changes_are_lifted_for_copy_out():
    out = _run_wrapped(
        "open('note.txt', 'w').write('hello')",
        {"workbench_files": [], "workbench_lazy": [],
         "workbench_limits": {"max_file_bytes": 4096, "max_total_bytes": 8192}},
    )
    assert [c["path"] for c in out["workbench_changes"]] == ["note.txt"]


def test_executor_level_failure_is_unchanged():
    from app.services.code_execution import _shape_code_result

    out = _shape_code_result({"status": "failed", "error": "No 'handler' function found"}, 1)
    assert out["error"] == "No 'handler' function found" and out["result"] is None
