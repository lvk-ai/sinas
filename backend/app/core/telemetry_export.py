"""The OpenTelemetry pipeline pieces between Sinas's spans and each exporter.

- `SinasSpanExporter` wraps every exporter. At export time (in the batch
  processor's thread, off the request path) it removes credentials from URL
  attributes and exception text, renames Sinas's LLM attributes to the
  exporter's preset (`PRESETS`), and, for an exporter that only wants Sinas's
  own spans, drops the others and re-parents what is left onto the nearest
  span it does receive.
- `SinasAncestry` records, as each span starts, which span that is.
- `OrphanClientSpanSampler` drops a database, cache or HTTP client span that
  has no parent: a worker's polling loop, a heartbeat. Each would otherwise
  be a trace of its own.
- `RedactingLogProcessor` / `OTLPLogHandler`: Python logging to OTLP logs,
  with the same redaction.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import Event, ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult
from opentelemetry.trace import SpanContext, SpanKind, Status

from app.middleware.request_logger import redact_path, redact_url_query

#: The instrumentation scope of the spans Sinas records itself.
SINAS_SCOPE = "sinas"

# ── Canonical attributes ────────────────────────────────────────────────────
#
# Sinas's own spans carry their LLM attributes under these names (what
# `telemetry.otel_attr(role)` returns). Each exporter's preset renames them on
# the way out, so one span can reach Langfuse, LangWatch and an APM at once,
# each in the names it understands.

CANONICAL: dict[str, str] = {
    "thread_id": "gen_ai.conversation.id",
    "user_id": "user.id",
    "span_type": "sinas.span.type",
    "input": "sinas.input",
    "output": "sinas.output",
    "labels": "sinas.labels",
}
_ROLE_OF = {key: role for role, key in CANONICAL.items()}
# Prompt/response content and function results: never sent under `none`.
_CONTENT_KEYS = frozenset({"sinas.input", "sinas.output", "function.result"})

#: Internal: the span id (hex) a span hangs under for an exporter that
#: receives only Sinas's own spans ("" for none). Never exported.
ANCESTOR_ATTR = "sinas.otel.ancestor"
#: Internal: carried in a queued job's trace context (`inject_job_context`),
#: the same for the job's spans in the worker. Not an HTTP header.
JOB_ANCESTOR_FIELD = "sinas-ancestor"
_JOB_ANCESTOR_KEY = otel_context.create_key("sinas-ancestor")

_LANGFUSE_TYPES = {"llm": "generation", "agent": "agent", "tool": "tool", "chain": "chain"}
_GENAI_OPERATIONS = {"llm": "chat", "agent": "invoke_agent", "tool": "execute_tool"}


def _labels_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    try:
        parsed = json.loads(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        return tuple(str(v) for v in parsed)
    return (str(value),) if value else ()


def _messages(role: str, value: Any) -> str:
    """Content as OTel GenAI `gen_ai.{input,output}.messages` (one text part)."""
    return json.dumps(
        [{"role": role, "parts": [{"type": "text", "content": value if isinstance(value, str) else str(value)}]}]
    )


def _preset_genai(roles: dict[str, Any], out: dict[str, Any]) -> None:
    span_type = roles.get("span_type")
    for role, key in (("thread_id", "gen_ai.conversation.id"), ("user_id", "user.id"),
                      ("span_type", "sinas.span.type"), ("labels", "sinas.labels")):
        if role in roles:
            out[key] = roles[role]
    if span_type in _GENAI_OPERATIONS:
        out.setdefault("gen_ai.operation.name", _GENAI_OPERATIONS[span_type])
    if span_type == "tool":
        if "input" in roles:
            out["gen_ai.tool.call.arguments"] = roles["input"]
        if "output" in roles:
            out["gen_ai.tool.call.result"] = roles["output"]
    else:
        if "input" in roles:
            out["gen_ai.input.messages"] = _messages("user", roles["input"])
        if "output" in roles:
            out["gen_ai.output.messages"] = _messages("assistant", roles["output"])
    if "gen_ai.system" in out:
        out.setdefault("gen_ai.provider.name", out["gen_ai.system"])


def _preset_langfuse(roles: dict[str, Any], out: dict[str, Any]) -> None:
    # https://langfuse.com/integrations/native/opentelemetry (property mapping)
    if "thread_id" in roles:
        out["langfuse.session.id"] = roles["thread_id"]
    if "user_id" in roles:
        out["langfuse.user.id"] = roles["user_id"]
    if roles.get("span_type") in _LANGFUSE_TYPES:
        out["langfuse.observation.type"] = _LANGFUSE_TYPES[roles["span_type"]]
    if "input" in roles:
        out["langfuse.observation.input"] = roles["input"]
    if "output" in roles:
        out["langfuse.observation.output"] = roles["output"]
    if "labels" in roles:
        out["langfuse.trace.tags"] = _labels_list(roles["labels"])


def _fixed_names(names: dict[str, str]) -> Callable[[dict[str, Any], dict[str, Any]], None]:
    def apply(roles: dict[str, Any], out: dict[str, Any]) -> None:
        for role, value in roles.items():
            out[names[role]] = value

    return apply


def _preset_none(roles: dict[str, Any], out: dict[str, Any]) -> None:
    for role in ("thread_id", "user_id", "span_type", "labels"):
        if role in roles:
            out[CANONICAL[role]] = roles[role]


PRESETS: dict[str, Callable[[dict[str, Any], dict[str, Any]], None]] = {
    "genai": _preset_genai,
    "langfuse": _preset_langfuse,
    # The pre-0.5.0 names, unchanged, for the dashboards built on them.
    "langwatch": _fixed_names({
        "thread_id": "langwatch.thread.id",
        "user_id": "langwatch.user.id",
        "span_type": "langwatch.span.type",
        "input": "langwatch.input",
        "output": "langwatch.output",
        "labels": "langwatch.labels",
    }),
    "generic_otel": _fixed_names({
        "thread_id": "gen_ai.thread.id",
        "user_id": "gen_ai.user.id",
        "span_type": "gen_ai.span.type",
        "input": "gen_ai.input",
        "output": "gen_ai.output",
        "labels": "gen_ai.labels",
    }),
    "none": _preset_none,
}


# ── Redaction ───────────────────────────────────────────────────────────────

_REDACTED = "***REDACTED***"
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Authorization schemes
    (re.compile(r"(?i)\b(bearer|basic|token)(\s+)[A-Za-z0-9._~+/=-]{8,}"), rf"\1\2{_REDACTED}"),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"), _REDACTED),
    # Provider-style keys: sk-..., sk-ant-..., sk-proj-..., pk-lf-..., sk-lf-...
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"), _REDACTED),
    # name=value / "name": "value" for credential-like names
    (
        re.compile(
            r"(?i)([\"']?\b(?:password|passwd|pwd|secret|client_secret|api[_-]?key|apikey"
            r"|access[_-]?token|refresh[_-]?token|id[_-]?token|token|auth|authorization"
            r"|x-api-key|private[_-]?key|encryption[_-]?key|sentry_key)\b[\"']?\s*[:=]\s*[\"']?)"
            r"([^\s\"',;&}]+)"
        ),
        rf"\1{_REDACTED}",
    ),
    # userinfo in URLs: scheme://user:pass@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1***@"),
]


def redact_text(text: str) -> str:
    """Text (a log message, an exception message or traceback) with
    credentials it may hold masked: query parameters named like credentials
    (as the request log masks them), authorization values, JWTs, API keys,
    `password=...`-style pairs and URL userinfo. Best effort, and a second
    line of defence: code should not log secrets in the first place."""
    if not text:
        return text
    text = redact_url_query(text)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_url(value: str) -> str:
    """A URL attribute without userinfo, credential query values or a
    credential path segment (`/watch/<secret>`)."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return _REDACTED
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "***@" + netloc.rsplit("@", 1)[-1]
    query = redact_url_query("?" + parts.query)[1:] if parts.query else ""
    return urlunsplit((parts.scheme, netloc, redact_path(parts.path), query, ""))


def _redact_query(value: str) -> str:
    return redact_url_query("?" + value)[1:]


def _redact_path_attr(value: str) -> str:
    path, sep, query = value.partition("?")
    return redact_path(path) + (redact_url_query("?" + query) if sep else "")


_URL_ATTRS: dict[str, Callable[[str], str]] = {
    "url.full": redact_url,
    "http.url": redact_url,
    "url.query": _redact_query,
    "url.path": _redact_path_attr,
    "http.target": _redact_path_attr,
}
_TEXT_ATTRS = frozenset({"exception.message", "exception.stacktrace"})


def _sanitize_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(attributes)
    for key, fix in _URL_ATTRS.items():
        value = out.get(key)
        if isinstance(value, str) and value:
            out[key] = fix(value)
    for key in _TEXT_ATTRS:
        value = out.get(key)
        if isinstance(value, str) and value:
            out[key] = redact_text(value)
    return out


# ── Span export ─────────────────────────────────────────────────────────────


def _is_sinas(span: ReadableSpan) -> bool:
    scope = span.instrumentation_scope
    return scope is not None and scope.name == SINAS_SCOPE


class SinasSpanExporter(SpanExporter):
    """An exporter, given each span as its preset and span scope want it."""

    def __init__(self, inner: SpanExporter, *, preset: str = "genai", spans: str = "all") -> None:
        self._inner = inner
        self._preset = PRESETS[preset]
        self._drop_content = preset == "none"
        self._sinas_only = spans == "sinas"

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            out = [
                self.transform(span)
                for span in spans
                if not self._sinas_only or _is_sinas(span)
            ]
        except Exception:  # never lose a batch to a transform bug
            logging.getLogger(__name__).exception("Span transform failed; batch dropped")
            return SpanExportResult.FAILURE
        if not out:
            return SpanExportResult.SUCCESS
        return self._inner.export(out)

    def transform(self, span: ReadableSpan) -> ReadableSpan:
        attributes = _sanitize_attributes(span.attributes or {})
        ancestor = attributes.pop(ANCESTOR_ATTR, None)

        roles: dict[str, Any] = {}
        for key in list(attributes):
            role = _ROLE_OF.get(key)
            if role is not None:
                roles[role] = attributes.pop(key)
        if self._drop_content:
            for key in _CONTENT_KEYS:
                attributes.pop(key, None)
            roles.pop("input", None)
            roles.pop("output", None)
        if roles:
            self._preset(roles, attributes)

        parent = span.parent
        if self._sinas_only and ancestor is not None and span.context is not None:
            parent = (
                SpanContext(
                    trace_id=span.context.trace_id,
                    span_id=int(ancestor, 16),
                    is_remote=True,
                    trace_flags=span.context.trace_flags,
                    trace_state=span.context.trace_state,
                )
                if ancestor
                else None
            )

        events = [
            Event(event.name, _sanitize_attributes(event.attributes or {}), event.timestamp)
            if event.attributes and _TEXT_ATTRS & set(event.attributes)
            else event
            for event in span.events
        ]
        status = span.status
        if status.description:
            status = Status(status.status_code, redact_text(status.description))

        return ReadableSpan(
            name=span.name,
            context=span.context,
            parent=parent,
            resource=span.resource,
            attributes=attributes,
            events=events,
            links=span.links,
            kind=span.kind,
            status=status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


def _ancestor_hex(span: trace.Span, context: Optional[otel_context.Context]) -> Optional[str]:
    """The span a child of `span` hangs under, for an exporter receiving only
    Sinas's spans: `span` itself if it is one, else what `span` hangs under.
    None when nothing is known (no internal attribute: a span from before
    telemetry started, or a remote parent)."""
    span_context = span.get_span_context()
    if not span_context.is_valid:
        return ""
    if isinstance(span, ReadableSpan) and span.instrumentation_scope is not None:
        if span.instrumentation_scope.name == SINAS_SCOPE:
            return format(span_context.span_id, "016x")
        value = (span.attributes or {}).get(ANCESTOR_ATTR)
        return value if isinstance(value, str) else ""
    # A remote parent: a queued job's says what it hung under in the process
    # that enqueued it; any other (a caller's traceparent) is kept as is.
    marker = otel_context.get_value(_JOB_ANCESTOR_KEY, context)
    if isinstance(marker, str):
        return marker
    return format(span_context.span_id, "016x")


class SinasAncestry(SpanProcessor):
    """Records on each span (internal attribute `ANCESTOR_ATTR`) the nearest
    span an exporter of Sinas's spans only will receive, so that exporter can
    re-parent onto it. Installed only when such an exporter is configured."""

    def on_start(self, span: Any, parent_context: Optional[otel_context.Context] = None) -> None:
        parent = trace.get_current_span(parent_context)
        ancestor = _ancestor_hex(parent, parent_context)
        span.set_attribute(ANCESTOR_ATTR, ancestor or "")

    def on_end(self, span: ReadableSpan) -> None:
        pass


def job_ancestor() -> Optional[str]:
    """For a job enqueued now: the span its spans hang under (see
    `SinasAncestry`), or None when not tracking."""
    return _ancestor_hex(trace.get_current_span(), None)


def with_job_ancestor(ctx: otel_context.Context, value: Any) -> otel_context.Context:
    if isinstance(value, str) and (value == "" or re.fullmatch(r"[0-9a-f]{16}", value)):
        return otel_context.set_value(_JOB_ANCESTOR_KEY, value, ctx)
    return ctx


class OrphanClientSpanSampler(Sampler):
    """Drops CLIENT spans that start with no parent; defers everything else.

    A query, Redis command or outgoing HTTP call made while serving a request
    or running a job is a child of that span and kept. One made by a polling
    loop or a heartbeat has no parent, and would be a one-span trace every few
    seconds per process: noise that costs storage and hides real traces.
    """

    def __init__(self, delegate: Sampler) -> None:
        self._delegate = delegate

    def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None, links=None, trace_state=None):
        if kind == SpanKind.CLIENT and not trace.get_current_span(parent_context).get_span_context().is_valid:
            return SamplingResult(Decision.DROP)
        return self._delegate.should_sample(
            parent_context, trace_id, name, kind, attributes, links, trace_state
        )

    def get_description(self) -> str:
        return f"OrphanClientSpanSampler{{{self._delegate.get_description()}}}"


# ── Logs ────────────────────────────────────────────────────────────────────


def _log_record_processor_base():
    from opentelemetry.sdk._logs import LogRecordProcessor

    return LogRecordProcessor


class RedactingLogProcessor(_log_record_processor_base()):  # type: ignore[misc]
    """First on the logger provider: every record, Sinas's and FastAPI's
    exception logs alike, has credentials masked in its body and string
    attributes before any exporter sees it."""

    def on_emit(self, log_record: Any) -> None:
        try:
            record = log_record.log_record
            if isinstance(record.body, str):
                record.body = redact_text(record.body)
            attributes = record.attributes
            if attributes:
                for key, value in list(attributes.items()):
                    if isinstance(value, str) and value:
                        attributes[key] = redact_text(value)
        except Exception:
            pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


# Records from these loggers are never exported: the export pipeline's own
# (a failing exporter logging about itself would loop) and HTTP client
# internals.
_NEVER_EXPORTED = ("opentelemetry", "urllib3", "requests", "httpcore", "httpcore2", "hpack", "grpc")
# Below WARNING, these only repeat what their spans record (httpx logs every
# request at INFO; SQLAlchemy's echo logs every statement).
_SPANS_SAY_IT = ("httpx", "httpx2", "sqlalchemy.engine")


def _severity(levelno: int) -> Any:
    """A Python log level as an OTel severity number (logs data model:
    DEBUG 5, INFO 9, WARN 13, ERROR 17, FATAL 21; levels between round down)."""
    from opentelemetry._logs import SeverityNumber

    for floor, number in (
        (logging.CRITICAL, 21), (logging.ERROR, 17), (logging.WARNING, 13),
        (logging.INFO, 9), (logging.DEBUG, 5),
    ):
        if levelno >= floor:
            return SeverityNumber(number + min(levelno - floor, 3))
    return SeverityNumber(1)


class OTLPLogHandler(logging.Handler):
    """Python logging to OTLP logs, correlated with the current span.

    Unlike the SDK's handler, a record's ad-hoc attributes (`extra=`) are not
    exported: only the message, level, logger, code location and exception.
    The message is redacted here and again by `RedactingLogProcessor`.
    """

    def __init__(self, logger_provider: Any, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self._provider = logger_provider

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_NEVER_EXPORTED):
            return
        if record.levelno < logging.WARNING and record.name.startswith(_SPANS_SAY_IT):
            return
        try:
            try:
                message = record.getMessage()
            except Exception:
                message = str(record.msg)
            attributes: dict[str, Any] = {
                "code.file.path": record.pathname,
                "code.function.name": record.funcName,
                "code.line.number": record.lineno,
                "logger.name": record.name,
            }
            exception = None
            if record.exc_info and record.exc_info[1] is not None:
                exception = record.exc_info[1]
            self._provider.get_logger(record.name).emit(
                timestamp=int(record.created * 1e9),
                observed_timestamp=time.time_ns(),
                context=otel_context.get_current(),
                severity_number=_severity(record.levelno),
                severity_text={"WARNING": "WARN", "CRITICAL": "FATAL"}.get(record.levelname, record.levelname),
                body=redact_text(message),
                attributes=attributes,
                exception=exception,
            )
        except Exception:
            # Logging must never raise into the caller, nor recurse.
            pass


class _HeaderWarningFilter(logging.Filter):
    """The SDK's header parser logs a malformed header pair verbatim; that
    pair may be a credential. Keep the warning, drop the value."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple("<redacted>" for _ in record.args) if isinstance(record.args, tuple) else ()
        return True


def protect_sdk_header_warnings() -> None:
    logger = logging.getLogger("opentelemetry.util.re")
    if not any(isinstance(f, _HeaderWarningFilter) for f in logger.filters):
        logger.addFilter(_HeaderWarningFilter())


__all__ = [
    "ANCESTOR_ATTR",
    "CANONICAL",
    "JOB_ANCESTOR_FIELD",
    "OTLPLogHandler",
    "OrphanClientSpanSampler",
    "PRESETS",
    "RedactingLogProcessor",
    "SINAS_SCOPE",
    "SinasAncestry",
    "SinasSpanExporter",
    "job_ancestor",
    "protect_sdk_header_warnings",
    "redact_text",
    "redact_url",
    "with_job_ancestor",
]
