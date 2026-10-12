"""Request and execution logging never blocks the event loop.

The ClickHouse client is synchronous, and every request inserted its log row
on the event loop, stalling every other request and stream in the process
while it ran. With ClickHouse unreachable, each request also tried to
reconnect on the loop, for up to the connect timeout. Now rows are buffered
and inserted in batches from a thread, and a failed connection waits before
trying again.
"""
import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

from app.services import clickhouse_logger as module
from app.services.clickhouse_logger import ClickHouseLogger


def _logger(client=None):
    with patch.object(module.settings, "clickhouse_host", "clickhouse"):
        logger = ClickHouseLogger()
    if client is not None:
        logger.client = client
    return logger


async def _log(logger, n=1):
    for i in range(n):
        await logger.log_request(
            request_id=f"r{i}", user_id=None, user_email=None, permission_used=None,
            has_permission=True, method="GET", path="/x", query_params={}, request_body=None,
            user_agent=None, referer=None, ip_address=None, status_code=200,
            response_time_ms=1, response_size_bytes=0,
        )


async def test_a_slow_insert_does_not_block_the_loop(monkeypatch):
    monkeypatch.setattr(module, "FLUSH_INTERVAL_S", 0.01)
    inserted, threads = [], []

    def slow_insert(table, rows, column_names):
        threads.append(threading.current_thread() is threading.main_thread())
        time.sleep(0.3)
        inserted.append((table, len(rows)))

    logger = _logger(client=MagicMock(insert=slow_insert))
    started = time.perf_counter()
    await _log(logger, 5)
    assert time.perf_counter() - started < 0.05  # buffered, not written

    # The loop keeps ticking while the insert runs.
    ticks = 0
    while not inserted:
        await asyncio.sleep(0.01)
        ticks += 1
    assert ticks >= 10
    assert inserted == [("request_logs", 5)]  # one batch
    assert threads == [False]


async def test_an_unreachable_clickhouse_is_retried_later_not_per_request(monkeypatch):
    monkeypatch.setattr(module, "FLUSH_INTERVAL_S", 0.01)
    with patch.object(module.clickhouse_connect, "get_client", side_effect=ConnectionError("down")) as get_client:
        logger = _logger()
        with patch.object(module.settings, "clickhouse_host", "clickhouse"):
            for _ in range(3):
                await _log(logger, 10)
                await logger.flush()
        assert get_client.call_count == 1
        assert not logger.available


async def test_a_failed_insert_backs_off_too(monkeypatch):
    client = MagicMock()
    client.insert.side_effect = OSError("connection reset")
    logger = _logger(client=client)
    await _log(logger)
    await logger.flush()
    assert logger.client is None and not logger.available
    with patch.object(module.clickhouse_connect, "get_client") as get_client:
        await _log(logger)
        await logger.flush()
        get_client.assert_not_called()


async def test_the_buffer_is_bounded(monkeypatch):
    monkeypatch.setattr(module, "MAX_BUFFERED_ROWS", 3)
    logger = _logger(client=MagicMock())
    await _log(logger, 5)
    assert len(logger._buffers["request_logs"]) == 3
    assert logger._dropped == 2
    await logger.aclose()


async def test_close_writes_what_is_buffered():
    client = MagicMock()
    logger = _logger(client=client)
    await _log(logger, 2)
    await logger.log_execution_end("e1", "completed", output_data={"ok": object()})
    await logger.aclose()
    tables = {c.args[0]: len(c.args[1]) for c in client.insert.call_args_list}
    assert tables == {"request_logs": 2, "execution_logs": 1}
    client.close.assert_called_once()


async def test_queries_run_off_the_loop():
    seen = []

    def query(sql, parameters):
        seen.append(threading.current_thread() is threading.main_thread())
        return MagicMock(result_rows=[(1,)])

    logger = _logger(client=MagicMock(query=query))
    assert await logger.get_execution_logs("e1") == [(1,)]
    assert seen == [False]
