"""ClickHouse-disabled mode (compact profile): empty CLICKHOUSE_HOST.

The chart's `clickhouse.enabled: false` sets CLICKHOUSE_HOST="" — the logger
must treat that as a deliberate disable: no connection attempts (not even
retries from `_ensure_client`), logging no-ops, queries return empty.
"""

from unittest.mock import patch

from app.services.clickhouse_logger import ClickHouseLogger


def _disabled_logger():
    with patch("app.services.clickhouse_logger.settings") as s:
        s.clickhouse_host = ""
        return ClickHouseLogger()


def test_empty_host_disables_without_connecting():
    with patch("app.services.clickhouse_logger.clickhouse_connect") as cc:
        logger = _disabled_logger()
        cc.get_client.assert_not_called()
    assert logger.client is None


def test_ensure_client_does_not_retry_when_disabled():
    logger = _disabled_logger()
    with patch("app.services.clickhouse_logger.clickhouse_connect") as cc:
        with patch("app.services.clickhouse_logger.settings") as s:
            s.clickhouse_host = ""
            assert logger._ensure_client() is False
            assert logger._ensure_client() is False
        cc.get_client.assert_not_called()


def test_query_logs_returns_empty_when_disabled():
    import asyncio

    logger = _disabled_logger()
    with patch("app.services.clickhouse_logger.settings") as s:
        s.clickhouse_host = ""
        rows = asyncio.run(logger.query_logs())
    assert rows == []
