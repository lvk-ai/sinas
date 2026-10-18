"""The chat archive format: one gzipped JSON-lines object per chat.

    line 1      {"type": "chat", "format": ..., "format_version": 1, "chat": {...}, ...}
    lines 2..   {"type": "message", ...} in (created_at, id) order,
                then {"type": "tool_result", ...}
    last line   {"type": "manifest", "counts": {...}, "sha256": <hex>}

The manifest's sha256 covers every byte before the manifest line (the
uncompressed JSON lines, newlines included), so a truncated or altered
archive is detected by `parse_archive`. Pure functions: no database, no
storage. See docs/design/chat-retention.md §4.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

FORMAT = "sinas.chat-archive"
FORMAT_VERSION = 1
CONTENT_TYPE = "application/x-ndjson"

CHAT = "chat"
MESSAGE = "message"
TOOL_RESULT = "tool_result"
MANIFEST = "manifest"

# What a line of each kind must carry (a restore reads these).
_REQUIRED = {
    MESSAGE: ("id", "role", "created_at"),
    TOOL_RESULT: ("id", "tool_call_id", "tool_name", "source", "created_at", "expires_at"),
}
_CHAT_REQUIRED = ("id", "user_id", "title", "created_at")

# A decompressed archive larger than this is refused (a gzip bomb, or not
# an archive): far above any real chat.
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024


class InvalidArchiveError(ValueError):
    """The bytes are not a valid, intact chat archive."""


def object_key(prefix: str, chat_id: Any, created_at: datetime) -> str:
    """`<prefix>/<yyyy>/<mm>/<chat_id>.jsonl.gz`, by the chat's creation
    month: stable, so archiving a chat again writes the same key."""
    prefix = (prefix or "").strip("/")
    tail = f"{created_at:%Y}/{created_at:%m}/{chat_id}.jsonl.gz"
    return f"{prefix}/{tail}" if prefix else tail


def jsonable(value: Any) -> Any:
    """JSON-safe copy: datetimes as ISO 8601, UUIDs as strings."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    return value


def _line(obj: dict[str, Any]) -> bytes:
    return (json.dumps(jsonable(obj), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def content_digest(chat: dict[str, Any], messages: list[dict], tool_results: list[dict]) -> str:
    """What the delete step compares: the chat's archived content, without
    the per-run header fields (archived_at, sinas_version, retention). Equal
    digest = the chat is still exactly what was archived."""
    h = hashlib.sha256()
    h.update(_line({"type": CHAT, "chat": chat}))
    for m in messages:
        h.update(_line({"type": MESSAGE, **m}))
    for r in tool_results:
        h.update(_line({"type": TOOL_RESULT, **r}))
    return h.hexdigest()


@dataclass
class BuiltArchive:
    body: bytes  # gzip
    sha256: str  # of the gzip bytes (object metadata)
    content_sha256: str  # the manifest's: of the uncompressed lines before it
    counts: dict[str, int]


def build_archive(
    header: dict[str, Any],
    chat: dict[str, Any],
    messages: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
    workbench: Optional[dict[str, Any]] = None,
) -> BuiltArchive:
    """The archive of one chat. `header` carries the run's fields
    (archived_at, sinas_version, retention); `chat` the chat's own."""
    lines = [
        _line({
            "type": CHAT,
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            **header,
            "chat": chat,
            "workbench": workbench or {"files": []},
        })
    ]
    lines += [_line({"type": MESSAGE, **m}) for m in messages]
    lines += [_line({"type": TOOL_RESULT, **r}) for r in tool_results]
    payload = b"".join(lines)
    content_sha = hashlib.sha256(payload).hexdigest()
    counts = {MESSAGE: len(messages), TOOL_RESULT: len(tool_results)}
    payload += _line({"type": MANIFEST, "counts": counts, "sha256": content_sha})
    # mtime=0: the same content compresses to the same bytes.
    body = gzip.compress(payload, compresslevel=6, mtime=0)
    return BuiltArchive(
        body=body, sha256=hashlib.sha256(body).hexdigest(), content_sha256=content_sha, counts=counts
    )


@dataclass
class ParsedArchive:
    header: dict[str, Any]
    chat: dict[str, Any]
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)


def _gunzip(data: bytes) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
            out = f.read(MAX_UNCOMPRESSED_BYTES + 1)
    except (OSError, EOFError) as e:
        raise InvalidArchiveError(f"not a gzip file, or truncated: {e}") from None
    if len(out) > MAX_UNCOMPRESSED_BYTES:
        raise InvalidArchiveError("archive is larger than any chat archive could be")
    return out


def parse_archive(data: bytes) -> ParsedArchive:
    """Parse and verify an archive: the gzip, every line's JSON and shape,
    the format version, unique ids, the manifest's counts and sha256. Raises
    InvalidArchiveError naming the first problem."""
    raw = _gunzip(data)
    if not raw.endswith(b"\n"):
        raise InvalidArchiveError("archive doesn't end with a complete line (truncated?)")
    lines = raw.split(b"\n")[:-1]
    if len(lines) < 2:
        raise InvalidArchiveError("archive has no chat line or no manifest")

    def load(n: int, line: bytes) -> dict[str, Any]:
        try:
            obj = json.loads(line)
        except ValueError:
            raise InvalidArchiveError(f"line {n}: not JSON") from None
        if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
            raise InvalidArchiveError(f"line {n}: not an archive record")
        return obj

    first = load(1, lines[0])
    if first["type"] != CHAT or first.get("format") != FORMAT:
        raise InvalidArchiveError("line 1: not a Sinas chat archive")
    if first.get("format_version") != FORMAT_VERSION:
        raise InvalidArchiveError(
            f"format version {first.get('format_version')!r} is not supported (this Sinas reads {FORMAT_VERSION})"
        )
    chat = first.get("chat")
    if not isinstance(chat, dict) or any(chat.get(k) in (None, "") for k in _CHAT_REQUIRED):
        raise InvalidArchiveError(f"line 1: chat record lacks one of {', '.join(_CHAT_REQUIRED)}")
    _uuid(chat["id"], "line 1: chat id")
    _uuid(chat["user_id"], "line 1: chat user_id")

    manifest = load(len(lines), lines[-1])
    if manifest["type"] != MANIFEST:
        raise InvalidArchiveError("the last line is not the manifest (truncated?)")
    covered = b"".join(line + b"\n" for line in lines[:-1])
    if hashlib.sha256(covered).hexdigest() != manifest.get("sha256"):
        raise InvalidArchiveError("checksum mismatch: the archive was altered or corrupted")

    parsed = ParsedArchive(
        header={k: v for k, v in first.items() if k not in ("chat",)}, chat=chat, manifest=manifest
    )
    seen: dict[str, set[str]] = {MESSAGE: set(), TOOL_RESULT: set()}
    for n, line in enumerate(lines[1:-1], start=2):
        obj = load(n, line)
        kind = obj["type"]
        if kind not in _REQUIRED:
            raise InvalidArchiveError(f"line {n}: unknown record type {kind!r}")
        missing = [k for k in _REQUIRED[kind] if obj.get(k) in (None, "")]
        if missing:
            raise InvalidArchiveError(f"line {n}: {kind} lacks {', '.join(missing)}")
        _uuid(obj["id"], f"line {n}: {kind} id")
        if obj["id"] in seen[kind]:
            raise InvalidArchiveError(f"line {n}: duplicate {kind} id {obj['id']}")
        seen[kind].add(obj["id"])
        record = {k: v for k, v in obj.items() if k != "type"}
        (parsed.messages if kind == MESSAGE else parsed.tool_results).append(record)

    counts = manifest.get("counts") or {}
    if counts.get(MESSAGE) != len(parsed.messages) or counts.get(TOOL_RESULT) != len(parsed.tool_results):
        raise InvalidArchiveError(
            f"manifest counts {counts} don't match the archive "
            f"({len(parsed.messages)} messages, {len(parsed.tool_results)} tool results)"
        )
    return parsed


def _uuid(value: Any, what: str) -> None:
    try:
        uuid.UUID(str(value))
    except ValueError:
        raise InvalidArchiveError(f"{what} is not a UUID") from None
