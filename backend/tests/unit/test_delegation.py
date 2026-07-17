"""Unit tests for agent-to-agent delegation plumbing (issue #90).

Covers the pure pieces: depth propagation/bounding, queue topology, and job
registration. The suspend/resume round-trip itself needs a live stack and is
exercised in integration testing.
"""

from app.core.config import settings
from app.queue.agent_jobs import (
    execute_agent_delegate_resume_job,
    execute_agent_message_job,
    execute_agent_resume_job,
)
from app.queue.worker import AgentWorkerSettings, SubAgentWorkerSettings
from app.services.delegation import child_depth_or_error, current_delegation_depth
from app.services.queue_service import AGENT_QUEUE, SUB_AGENT_QUEUE


def test_child_depth_increments_from_context():
    token = current_delegation_depth.set(0)
    try:
        depth, err = child_depth_or_error()
        assert depth == 1 and err is None
    finally:
        current_delegation_depth.reset(token)


def test_child_depth_bounded():
    token = current_delegation_depth.set(settings.agent_max_delegation_depth)
    try:
        depth, err = child_depth_or_error()
        assert err is not None
        assert str(settings.agent_max_delegation_depth) in err
    finally:
        current_delegation_depth.reset(token)


def test_depth_limit_zero_disables_bound():
    original = settings.agent_max_delegation_depth
    token = current_delegation_depth.set(99)
    try:
        settings.agent_max_delegation_depth = 0
        _, err = child_depth_or_error()
        assert err is None
    finally:
        settings.agent_max_delegation_depth = original
        current_delegation_depth.reset(token)


def test_queue_topology():
    # Two distinct queues, each with its own worker settings.
    assert AgentWorkerSettings.queue_name == AGENT_QUEUE == "sinas:queue:agents"
    assert (
        SubAgentWorkerSettings.queue_name
        == SUB_AGENT_QUEUE
        == "sinas:queue:agents:sub"
    )
    assert SubAgentWorkerSettings.max_jobs == settings.queue_agent_sub_concurrency


def test_all_agent_jobs_registered_on_both_workers():
    # A delegated child (sub queue) can itself suspend and resume, so every
    # agent job type must be runnable on both queues.
    for job in (
        execute_agent_message_job,
        execute_agent_resume_job,
        execute_agent_delegate_resume_job,
    ):
        assert job in AgentWorkerSettings.functions
        assert job in SubAgentWorkerSettings.functions


def test_delegate_mode_defaults_to_block():
    # Suspend-on-delegate is opt-in; default behavior is unchanged.
    assert settings.agent_delegate_mode == "block"
    assert settings.agent_subagent_queue is True
