"""Queued jobs start promptly.

arq's default poll delay, 0.5 s, added up to half a second before every
queued job started: agent messages on keep-alive chats, and every function
an agent's tool call runs.
"""
from arq.worker import create_worker

from app.core.config import settings
from app.queue.worker import AgentWorkerSettings, PipelineWorkerSettings, SubAgentWorkerSettings, WorkerSettings


def test_every_worker_polls_its_queue_often():
    for cls in (WorkerSettings, AgentWorkerSettings, SubAgentWorkerSettings, PipelineWorkerSettings):
        worker = create_worker(cls, handle_signals=False)
        assert worker.poll_delay_s == settings.queue_poll_delay <= 0.1, cls.__name__
