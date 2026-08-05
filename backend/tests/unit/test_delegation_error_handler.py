"""The delegation failure handler must report the real error.

execute_agent_tool's except block used to log f"...{tool_name}..." with
tool_name not in scope, so any failure in the delegation path surfaced as
{"error": "name 'tool_name' is not defined"} instead of the actual cause.
These tests pin the fixed behavior: the underlying exception's message is
what reaches the caller.

Run from the backend directory:
`python -m pytest tests/unit/test_delegation_error_handler.py`
"""

import asyncio
from unittest.mock import MagicMock

import app.services.tool_execution as te


def _run(coro):
    return asyncio.run(coro)


def test_delegation_failure_returns_the_real_error(monkeypatch):
    async def boom(db, user_id, agent_id_str, arguments):
        raise ValueError("upstream boom")

    monkeypatch.setattr(te, "prepare_agent_delegation", boom)
    result = _run(
        te.execute_agent_tool(
            db=MagicMock(),
            chat=MagicMock(),
            user_id="u1",
            user_token="t1",
            agent_id_str="ns__agent",
            arguments={"prompt": "x"},
            create_chat_with_agent_fn=MagicMock(),
        )
    )
    assert result == {"error": "upstream boom"}


def test_delegation_failure_is_not_masked_by_name_error(monkeypatch):
    async def boom(db, user_id, agent_id_str, arguments):
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(te, "prepare_agent_delegation", boom)
    result = _run(
        te.execute_agent_tool(
            db=MagicMock(),
            chat=MagicMock(),
            user_id="u1",
            user_token="t1",
            agent_id_str="ns__agent",
            arguments={},
            create_chat_with_agent_fn=MagicMock(),
        )
    )
    assert "tool_name" not in result["error"]
    assert result["error"] == "redis unavailable"
