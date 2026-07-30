"""Unit tests for structure-aware tool-result truncation.

No network, no DB: `truncate_tool_result` is a pure function. The contract:
output never exceeds max_size, JSON in means JSON out (with a machine-readable
{"_truncated": ...} marker when trimmed), and non-JSON falls back to a clean
text cut with the visible suffix.
"""
import json

from app.services.tool_execution import (
    TOOL_RESULT_SIZE_OVERRIDES,
    truncate_tool_result,
)
from app.core.config import settings

MAX = 2000  # small budget so fixtures stay readable


def _rows(n, pad=120):
    return [{"id": i, "reason": "x" * pad} for i in range(n)]


def test_small_result_is_untouched():
    content = json.dumps(_rows(3))
    assert truncate_tool_result(content, MAX) == content


def test_oversized_list_keeps_whole_elements_and_marks():
    content = json.dumps(_rows(50))
    out = truncate_tool_result(content, MAX)
    assert len(out) <= MAX
    parsed = json.loads(out)  # valid JSON, not a mid-string cut
    *items, marker = parsed
    assert marker["_truncated"] is True
    assert marker["returned"] == len(items)
    assert marker["total"] == 50
    # every surviving element is intact
    assert all(set(i) == {"id", "reason"} for i in items)
    assert [i["id"] for i in items] == list(range(len(items)))


def test_dict_wrapped_body_list_is_trimmed_in_place():
    content = json.dumps(
        {"status_code": 200, "headers": {"server": "x"}, "body": _rows(50)}
    )
    out = truncate_tool_result(content, MAX)
    assert len(out) <= MAX
    parsed = json.loads(out)
    assert parsed["status_code"] == 200  # envelope survives
    assert parsed["_truncated"]["field"] == "body"
    assert parsed["_truncated"]["returned"] == len(parsed["body"])
    assert parsed["_truncated"]["total"] == 50
    assert all(set(i) == {"id", "reason"} for i in parsed["body"])


def test_dict_with_dominant_string_is_clipped_not_broken():
    content = json.dumps(
        {"status_code": 200, "body": {"version": 1, "content": "line\n" * 1000}}
    )
    out = truncate_tool_result(content, MAX)
    assert len(out) <= MAX
    parsed = json.loads(out)
    assert parsed["body"]["version"] == 1
    assert parsed["_truncated"]["field"] == "content"
    assert parsed["_truncated"]["returned"] < parsed["_truncated"]["total"]
    assert len(parsed["body"]["content"]) < 5000


def test_non_json_falls_back_to_clean_text_cut():
    content = "word " * 2000
    out = truncate_tool_result(content, MAX)
    assert len(out) <= MAX
    assert out.endswith("[... result truncated]")
    # boundary cut: never ends mid-word before the suffix
    assert not out.removesuffix("\n\n[... result truncated]").endswith("wor")


def test_default_budget_and_override_registry():
    assert settings.tool_result_max_size == 50000
    assert TOOL_RESULT_SIZE_OVERRIDES == {}
