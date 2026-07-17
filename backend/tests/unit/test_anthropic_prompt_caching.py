"""Unit tests for Anthropic prompt caching (cache_control breakpoints + usage).

No network calls: these exercise the request-mutation hook
(`_apply_cache_control`) and usage extraction directly.
"""
from types import SimpleNamespace

from app.providers import AnthropicProvider

EPHEMERAL = {"type": "ephemeral"}


def _params(**overrides):
    params = {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.7,
        "max_tokens": 100,
    }
    params.update(overrides)
    return params


def _provider(**kwargs):
    return AnthropicProvider(api_key="test-key", **kwargs)


def test_caching_enabled_by_default():
    assert _provider().enable_prompt_caching is True
    assert _provider(enable_prompt_caching=False).enable_prompt_caching is False


def test_marks_system_last_tool_and_last_message():
    params = _params(
        system="You are a helpful agent.",
        tools=[
            {"name": "search", "description": "", "input_schema": {}},
            {"name": "fetch", "description": "", "input_schema": {}},
        ],
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ],
    )
    _provider()._apply_cache_control(params)

    # System becomes a block list with a breakpoint on it
    assert params["system"] == [
        {"type": "text", "text": "You are a helpful agent.", "cache_control": EPHEMERAL}
    ]
    # Only the LAST tool is marked
    assert "cache_control" not in params["tools"][0]
    assert params["tools"][1]["cache_control"] == EPHEMERAL
    # Only the LAST message is marked, converted to block form
    assert params["messages"][0]["content"] == "first"
    assert params["messages"][2]["content"] == [
        {"type": "text", "text": "second", "cache_control": EPHEMERAL}
    ]


def test_marks_last_block_of_list_content_without_mutating_input():
    original_blocks = [
        {"type": "tool_result", "tool_use_id": "tc_1", "content": "result one"},
        {"type": "tool_result", "tool_use_id": "tc_2", "content": "result two"},
    ]
    params = _params(messages=[{"role": "user", "content": original_blocks}])
    _provider()._apply_cache_control(params)

    marked = params["messages"][0]["content"]
    assert "cache_control" not in marked[0]
    assert marked[1]["cache_control"] == EPHEMERAL
    # The caller's block objects must not be mutated (they may be reused
    # across requests; stale markers would accumulate breakpoints).
    assert "cache_control" not in original_blocks[1]


def test_no_tools_no_system_still_marks_last_message():
    params = _params()
    _provider()._apply_cache_control(params)
    assert params["messages"][0]["content"] == [
        {"type": "text", "text": "hello", "cache_control": EPHEMERAL}
    ]
    assert "tools" not in params
    assert "system" not in params


def test_empty_content_left_untouched():
    params = _params(messages=[{"role": "user", "content": ""}])
    _provider()._apply_cache_control(params)
    assert params["messages"][0]["content"] == ""


def test_at_most_three_breakpoints():
    params = _params(
        system="sys",
        tools=[{"name": "t", "description": "", "input_schema": {}}],
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
        ],
    )
    _provider()._apply_cache_control(params)

    marker_count = 0
    for tool in params["tools"]:
        marker_count += "cache_control" in tool
    for block in params["system"]:
        marker_count += "cache_control" in block
    for msg in params["messages"]:
        for block in msg["content"]:
            marker_count += "cache_control" in block
    assert marker_count == 3  # Anthropic allows max 4


def test_extract_usage_includes_cache_tokens():
    response = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=9000,
            cache_creation_input_tokens=400,
        )
    )
    usage = _provider().extract_usage(response)
    # prompt_tokens includes cached portions (input_tokens excludes them)
    assert usage["prompt_tokens"] == 9500
    assert usage["completion_tokens"] == 50
    assert usage["total_tokens"] == 9550
    assert usage["cache_read_tokens"] == 9000
    assert usage["cache_write_tokens"] == 400


def test_extract_usage_without_cache_fields():
    response = SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=50))
    usage = _provider().extract_usage(response)
    assert usage["prompt_tokens"] == 100
    assert usage["total_tokens"] == 150
    assert usage["cache_read_tokens"] == 0
    assert usage["cache_write_tokens"] == 0
