"""Retention policy: what the settings say, per agent, and when runs are due."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Optional
from urllib.parse import urlsplit

from app.core.config import settings

NEVER = "never"
# Longest retention an agent can set (100 years); the console caps input at
# the same value. Anything longer is a typo, and 0 / "never" says forever.
MAX_RETENTION_DAYS = 36500
GLOBAL = "global"
AGENT = "agent"


def normalize_retention_days(value: Any) -> Optional[int]:
    """An agent's retentionDays as stored: None = the deployment default,
    0 = keep forever, N > 0 = N days. Accepts "never" (config's spelling of
    0) and integral strings; anything else is an error."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("retentionDays must be a number of days, 0 or 'never'")
    if isinstance(value, str):
        text = value.strip().lower()
        if text == NEVER:
            return 0
        if not text.lstrip("-").isdigit():
            raise ValueError("retentionDays must be a number of days, 0 or 'never'")
        value = int(text)
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("retentionDays must be a whole number of days")
        value = int(value)
    if not isinstance(value, int):
        raise ValueError("retentionDays must be a number of days, 0 or 'never'")
    if value < 0:
        raise ValueError("retentionDays can't be negative (0 or 'never' keeps chats forever)")
    if value > MAX_RETENTION_DAYS:
        raise ValueError(
            f"retentionDays can be at most {MAX_RETENTION_DAYS} (100 years); use 0 or 'never' to keep chats forever"
        )
    return value


def effective_days(agent_days: Optional[int], global_days: Optional[int]) -> tuple[int, str]:
    """(days, source) for a chat of an agent with `agent_days`: the agent's
    value when set (0 = keep forever), else the global default (unset or 0
    = keep forever). days == 0 means the chat is kept."""
    if agent_days is not None:
        return max(int(agent_days), 0), AGENT
    return max(int(global_days or 0), 0), GLOBAL


@dataclass(frozen=True)
class RetentionPolicy:
    enabled: bool
    global_days: int
    action: str  # archive | delete
    schedule: str
    batch_size: int
    max_chats_per_run: int
    max_run_seconds: int

    @classmethod
    def from_settings(cls) -> "RetentionPolicy":
        return cls(
            enabled=bool(settings.chat_retention_enabled),
            global_days=max(int(settings.chat_retention_days or 0), 0),
            action=settings.chat_retention_action,
            schedule=settings.chat_retention_schedule,
            batch_size=max(int(settings.chat_retention_batch_size or 200), 1),
            max_chats_per_run=max(int(settings.chat_retention_max_chats_per_run or 0), 0),
            max_run_seconds=max(int(settings.chat_retention_max_run_seconds or 0), 0),
        )

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "default_days": self.global_days or None,
            "action": self.action,
            "schedule": self.schedule,
            "batch_size": self.batch_size,
            "max_chats_per_run": self.max_chats_per_run or None,
            "max_run_seconds": self.max_run_seconds or None,
        }


def destination_configured() -> bool:
    return bool((settings.chat_archive_s3_bucket or "").strip())


def describe_destination() -> dict[str, Any]:
    """The archive destination as admins may see it: where, never with what
    credentials (only whether static keys or the default chain are used)."""
    if not destination_configured():
        return {"configured": False}
    endpoint = settings.chat_archive_s3_endpoint_url
    shown_endpoint = None
    if endpoint:
        # Scheme and host only: a URL could carry userinfo or a query.
        parts = urlsplit(endpoint)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        shown_endpoint = f"{parts.scheme}://{host}" if parts.scheme else host
    return {
        "configured": True,
        "bucket": settings.chat_archive_s3_bucket,
        "prefix": archive_prefix(),
        "endpoint": shown_endpoint or "AWS S3",
        "region": settings.chat_archive_s3_region,
        "sse_kms": bool(settings.chat_archive_s3_kms_key_id),
        "credentials": "static keys" if settings.chat_archive_s3_access_key_id else "default chain",
    }


def archive_prefix() -> str:
    return (settings.chat_archive_s3_prefix or "").strip("/")


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def schedule_error(schedule: str) -> Optional[str]:
    from croniter import croniter

    if not croniter.is_valid(schedule or ""):
        return f"CHAT_RETENTION_SCHEDULE '{schedule}' is not a valid cron expression"
    return None


def previous_fire(schedule: str, now: datetime) -> datetime:
    """The most recent time at or before `now` the schedule fired (UTC)."""
    from croniter import croniter

    return croniter(schedule, now.astimezone(UTC)).get_prev(datetime)


def next_fire(schedule: str, now: datetime) -> datetime:
    from croniter import croniter

    return croniter(schedule, now.astimezone(UTC)).get_next(datetime)


def run_is_due(
    schedule: str,
    now: datetime,
    last_scheduled_start: Optional[datetime],
    last_run_status: Optional[str],
) -> bool:
    """Whether the cron tick should start a run: the schedule fired since the
    last scheduled run started, or the last run (`last_run_status`, of any
    trigger: scheduled, Run now or the CLI) stopped before the end, at a
    budget or interrupted, so it continues rather than waiting for the next
    fire."""
    from app.models.retention_run import INTERRUPTED, PARTIAL

    if last_run_status in (PARTIAL, INTERRUPTED):
        return True
    fired = previous_fire(schedule, now)
    if last_scheduled_start is None:
        # Never ran: wait for the schedule (switching retention on in the
        # afternoon doesn't start deleting then; "Run now" does that).
        return fired > now - TICK_WINDOW
    return fired > last_scheduled_start


# The cron tick runs every 10 minutes; a fire within this window counts as
# "just fired" for a deployment that never ran.
TICK_WINDOW = timedelta(minutes=15)
