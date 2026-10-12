"""Queued jobs start promptly.

arq's default poll delay, 0.5 s, added up to half a second before every
queued job started: agent messages on keep-alive chats, and every function
an agent's tool call runs.
"""
from arq.worker import create_worker

from app.core.config import Settings, settings
from app.queue.worker import AgentWorkerSettings, PipelineWorkerSettings, SubAgentWorkerSettings, WorkerSettings


def test_the_default_is_short():
    assert Settings.model_fields["queue_poll_delay"].default == 0.05


def test_every_worker_uses_the_setting():
    for cls in (WorkerSettings, AgentWorkerSettings, SubAgentWorkerSettings, PipelineWorkerSettings):
        worker = create_worker(cls, handle_signals=False)
        assert worker.poll_delay_s == settings.queue_poll_delay, cls.__name__
