"""Chat retention: policy resolution, the schedule, and the archive format.

Pure functions, no database. The job itself is in test_chat_retention_job.py.
"""

import gzip
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.models.retention_run import COMPLETED, PARTIAL
from app.services.retention import archive as fmt
from app.services.retention.policy import (
    describe_destination,
    effective_days,
    normalize_retention_days,
    run_is_due,
    schedule_error,
)


class TestPolicyResolution:
    def test_agent_value_wins(self):
        assert effective_days(10, 90) == (10, "agent")

    def test_agent_never_keeps_even_with_a_global_default(self):
        assert effective_days(0, 90) == (0, "agent")

    def test_unset_agent_uses_global(self):
        assert effective_days(None, 90) == (90, "global")

    def test_global_unset_or_zero_keeps(self):
        assert effective_days(None, 0) == (0, "global")
        assert effective_days(None, None) == (0, "global")

    def test_agent_override_works_without_a_global_default(self):
        assert effective_days(7, 0) == (7, "agent")

    @pytest.mark.parametrize(
        "given,stored",
        [(None, None), (0, 0), ("never", 0), ("NEVER", 0), (30, 30), ("30", 30), (30.0, 30)],
    )
    def test_normalize(self, given, stored):
        assert normalize_retention_days(given) == stored

    @pytest.mark.parametrize("bad", [-1, "-3", "soon", 1.5, True, [], {}])
    def test_normalize_rejects(self, bad):
        with pytest.raises(ValueError):
            normalize_retention_days(bad)


class TestSchedule:
    NOW = datetime(2026, 10, 18, 3, 35, tzinfo=UTC)  # 5 minutes after the 03:30 fire

    def test_invalid_schedule_is_reported(self):
        assert schedule_error("not a cron") is not None
        assert schedule_error("30 3 * * *") is None

    def test_due_after_a_fire_since_the_last_run(self):
        yesterday = self.NOW - timedelta(days=1)
        assert run_is_due("30 3 * * *", self.NOW, yesterday, COMPLETED)

    def test_not_due_twice_for_one_fire(self):
        started = self.NOW.replace(minute=31)
        assert not run_is_due("30 3 * * *", self.NOW, started, COMPLETED)

    def test_partial_run_continues_on_the_next_tick(self):
        started = self.NOW.replace(minute=31)
        assert run_is_due("30 3 * * *", self.NOW, started, PARTIAL)

    def test_first_run_waits_for_the_schedule(self):
        afternoon = datetime(2026, 10, 18, 14, 0, tzinfo=UTC)
        assert not run_is_due("30 3 * * *", afternoon, None, None)
        assert run_is_due("30 3 * * *", self.NOW, None, None)


class TestDestinationIsShownWithoutCredentials:
    def test_never_shows_keys(self, monkeypatch):
        monkeypatch.setattr(settings, "chat_archive_s3_bucket", "archive-bucket")
        monkeypatch.setattr(settings, "chat_archive_s3_endpoint_url", "https://user:pw@minio.internal:9000/x?sig=1")
        monkeypatch.setattr(settings, "chat_archive_s3_access_key_id", "AKIAEXAMPLEKEYID")
        monkeypatch.setattr(settings, "chat_archive_s3_secret_access_key", "example-secret-value")
        monkeypatch.setattr(settings, "chat_archive_s3_session_token", "example-session-token")
        shown = describe_destination()
        text = json.dumps(shown)
        assert shown["configured"] is True
        assert shown["endpoint"] == "https://minio.internal:9000"
        assert shown["credentials"] == "static keys"
        for secret in ("AKIAEXAMPLEKEYID", "example-secret-value", "example-session-token", "pw", "sig"):
            assert secret not in text

    def test_unconfigured(self, monkeypatch):
        monkeypatch.setattr(settings, "chat_archive_s3_bucket", None)
        assert describe_destination() == {"configured": False}


def _sample():
    chat_id = str(uuid.uuid4())
    chat = {
        "id": chat_id,
        "user_id": str(uuid.uuid4()),
        "user_email": "owner@example.com",
        "agent_namespace": "support",
        "agent_name": "triage",
        "title": "A chat",
        "archived": False,
        "created_at": "2026-01-05T10:00:00+00:00",
        "updated_at": "2026-01-05T10:05:00+00:00",
    }
    messages = [
        {"id": str(uuid.uuid4()), "role": "user", "content": "hello é ✓", "tool_calls": None,
         "tool_call_id": None, "name": None, "created_at": "2026-01-05T10:00:01+00:00"},
        {"id": str(uuid.uuid4()), "role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "function": {"name": "x", "arguments": "{}"}}],
         "tool_call_id": None, "name": None, "created_at": "2026-01-05T10:00:02+00:00"},
    ]
    results = [
        {"id": str(uuid.uuid4()), "tool_call_id": "call_1", "tool_name": "x", "source": "agent",
         "result": {"ok": True}, "created_at": "2026-01-05T10:00:03+00:00",
         "expires_at": "2026-02-05T10:00:03+00:00"},
    ]
    return chat, messages, results


class TestArchiveFormat:
    def test_key_is_by_creation_month_and_stable(self):
        created = datetime(2026, 1, 5, tzinfo=UTC)
        assert fmt.object_key("/sinas/chats/", "abc", created) == "sinas/chats/2026/01/abc.jsonl.gz"
        assert fmt.object_key("", "abc", created) == "2026/01/abc.jsonl.gz"

    def test_round_trip(self):
        chat, messages, results = _sample()
        built = fmt.build_archive({"sinas_version": "0.4.0", "archived_at": "x"}, chat, messages, results)
        parsed = fmt.parse_archive(built.body)
        assert parsed.chat == chat
        assert parsed.messages == messages
        assert parsed.tool_results == results
        assert parsed.manifest["counts"] == {"message": 2, "tool_result": 1}
        assert parsed.manifest["sha256"] == built.content_sha256
        assert parsed.header["format_version"] == fmt.FORMAT_VERSION

    def test_layout(self):
        chat, messages, results = _sample()
        built = fmt.build_archive({}, chat, messages, results)
        lines = gzip.decompress(built.body).decode().splitlines()
        types = [json.loads(line)["type"] for line in lines]
        assert types == ["chat", "message", "message", "tool_result", "manifest"]

    def test_same_content_same_bytes(self):
        chat, messages, results = _sample()
        a = fmt.build_archive({"archived_at": "t"}, chat, messages, results)
        b = fmt.build_archive({"archived_at": "t"}, chat, messages, results)
        assert a.body == b.body

    def test_tampering_is_detected(self):
        chat, messages, results = _sample()
        raw = gzip.decompress(fmt.build_archive({}, chat, messages, results).body)
        altered = raw.replace(b"hello", b"HELLO")
        with pytest.raises(fmt.InvalidArchiveError, match="checksum"):
            fmt.parse_archive(gzip.compress(altered))

    def test_truncation_is_detected(self):
        chat, messages, results = _sample()
        raw = gzip.decompress(fmt.build_archive({}, chat, messages, results).body)
        lines = raw.split(b"\n")
        with pytest.raises(fmt.InvalidArchiveError):
            fmt.parse_archive(gzip.compress(b"\n".join(lines[:-2]) + b"\n"))  # manifest gone
        with pytest.raises(fmt.InvalidArchiveError):
            fmt.parse_archive(fmt.build_archive({}, chat, messages, results).body[:-10])

    def test_not_an_archive(self):
        with pytest.raises(fmt.InvalidArchiveError):
            fmt.parse_archive(b"plain bytes")
        with pytest.raises(fmt.InvalidArchiveError):
            fmt.parse_archive(gzip.compress(b'{"type": "chat"}\n{"type": "manifest"}\n'))

    def test_unsupported_version(self):
        chat, messages, results = _sample()
        raw = gzip.decompress(fmt.build_archive({}, chat, messages, results).body)
        with pytest.raises(fmt.InvalidArchiveError, match="version"):
            fmt.parse_archive(gzip.compress(raw.replace(b'"format_version":1', b'"format_version":99')))

    def test_digest_ignores_run_fields_but_not_content(self):
        chat, messages, results = _sample()
        d1 = fmt.content_digest(chat, messages, results)
        assert d1 == fmt.content_digest(dict(chat), list(messages), list(results))
        changed = [dict(messages[0], content="other"), messages[1]]
        assert fmt.content_digest(chat, changed, results) != d1
