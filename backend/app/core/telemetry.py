"""OpenTelemetry for Sinas: vendor-neutral, opt-in, any number of OTLP backends.

Off by default (`OTEL_ENABLED`). Off, nothing is installed: no tracer
provider, no instrumentation, and Sinas's own spans are the OpenTelemetry
API's no-ops. On, each process (`init_telemetry(process)`) gets:

- a tracer provider whose sampler is the standard one (`OTEL_TRACES_SAMPLER`,
  default parent-based always-on) minus parentless client spans (see
  `OrphanClientSpanSampler`), and whose resource carries `service.name`,
  `sinas.process` and `OTEL_RESOURCE_ATTRIBUTES`;
- one batch span processor per exporter (`telemetry_config`), each behind a
  `SinasSpanExporter` that applies its attribute preset and redaction;
- instrumentation of SQLAlchemy, Redis and outgoing HTTP (httpx, httpx2).
  Incoming requests are traced by FastAPI itself (its native OpenTelemetry
  support, `fastapi_telemetry_config`), not by a contrib instrumentation;
- optionally, Python logging exported as OTLP logs (`OTEL_LOGS_ENABLED`).

LLM provider calls are recorded by Sinas's `llm.call` spans, not as HTTP
client spans, and carry trace context only when the provider is configured
to (`propagate_context`, `providers.clients.guard_trace_context`).
"""
import asyncio
import functools
import logging
import os
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, Optional, TypeVar

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import Status, StatusCode

from app.core.config import settings

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

SINAS_SCOPE = "sinas"

# Canonical names of the LLM attributes on Sinas's spans; each exporter's
# preset renames them on export (telemetry_export.PRESETS). Kept in step with
# telemetry_export.CANONICAL, which is not imported here so that a disabled
# process never loads the SDK.
_CANONICAL = {
    "thread_id": "gen_ai.conversation.id",
    "user_id": "user.id",
    "span_type": "sinas.span.type",
    "input": "sinas.input",
    "output": "sinas.output",
    "labels": "sinas.labels",
}


def otel_attr(role: str) -> str:
    """The attribute name for an LLM role on Sinas's spans.

    Roles: thread_id, user_id, span_type, input, output, labels. The name is
    canonical; what each backend receives depends on its exporter's preset
    (`genai`, `langfuse`, `langwatch`, `none`).
    """
    return _CANONICAL[role]


@dataclass
class _State:
    enabled: bool = False
    process: str = ""
    tracer_provider: Any = None
    logger_provider: Any = None
    log_handler: Optional[logging.Handler] = None
    instrumented: list[Any] = field(default_factory=list)
    unwrap: list[Callable[[], None]] = field(default_factory=list)
    track_ancestry: bool = False


_state: Optional[_State] = None


def is_enabled() -> bool:
    return _state is not None and _state.enabled


def get_tracer(name: str = SINAS_SCOPE) -> trace.Tracer:
    """A tracer; the API's no-op one while telemetry is off."""
    return trace.get_tracer(name)


# ── Init ────────────────────────────────────────────────────────────────────


def _standard_preset() -> str:
    """The preset of the OTEL_EXPORTER_OTLP_* exporter: OTEL_PRESET; failing
    that, an explicitly set OTEL_VENDOR (as before 0.5.0); else genai."""
    fields = settings.model_fields_set
    if "otel_preset" not in fields and "otel_vendor" in fields:
        return settings.otel_vendor
    return settings.otel_preset


def configured_exporters(env: Optional[dict[str, str]] = None):
    from app.core.telemetry_config import resolve_exporters

    return resolve_exporters(
        os.environ if env is None else env,
        exporters_json=settings.otel_exporters,
        standard_preset=_standard_preset(),
        legacy_endpoint=settings.otel_exporter_endpoint,
        legacy_headers=settings.otel_exporter_headers,
        legacy_vendor=settings.otel_vendor,
    )


def build_resource(process: str):
    """service.name per `OTEL_SERVICE_NAME` (`{process}` substituted), else
    the one `OTEL_RESOURCE_ATTRIBUTES` gives, else "sinas"; plus
    `sinas.process` and `service.version`."""
    from opentelemetry.sdk.resources import Resource

    from app import __version__

    detected = Resource.create({"sinas.process": process, "service.version": __version__})
    name = settings.otel_service_name
    if not name:
        current = str(detected.attributes.get("service.name") or "")
        name = current if current and not current.startswith("unknown_service") else "sinas"
    name = name.replace("{process}", process)
    return detected.merge(Resource({"service.name": name}))


def _span_exporter(config):
    from app.core.telemetry_export import SinasSpanExporter

    kwargs: dict[str, Any] = {}
    if config.source != "standard":
        kwargs["endpoint"] = config.traces_endpoint
        kwargs["headers"] = dict(config.headers or {})
        if config.timeout is not None:
            kwargs["timeout"] = config.timeout
    if config.protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as GrpcSpanExporter

        if config.source != "standard":
            if config.insecure is not None:
                kwargs["insecure"] = config.insecure
            if config.compression:
                kwargs["compression"] = _grpc_compression(config.compression)
        inner = GrpcSpanExporter(**kwargs)
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        if config.source != "standard" and config.compression:
            kwargs["compression"] = _http_compression(config.compression)
        inner = OTLPSpanExporter(**kwargs)
    return SinasSpanExporter(inner, preset=config.preset, spans=config.spans)


def _log_exporter(config):
    kwargs: dict[str, Any] = {}
    if config.source != "standard":
        kwargs["endpoint"] = config.logs_endpoint
        kwargs["headers"] = dict(config.headers or {})
        if config.timeout is not None:
            kwargs["timeout"] = config.timeout
    if config.logs_protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter as GrpcLogExporter

        if config.source != "standard":
            if config.insecure is not None:
                kwargs["insecure"] = config.insecure
            if config.compression:
                kwargs["compression"] = _grpc_compression(config.compression)
        return GrpcLogExporter(**kwargs)
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

    if config.source != "standard" and config.compression:
        kwargs["compression"] = _http_compression(config.compression)
    return OTLPLogExporter(**kwargs)


def _http_compression(value: str):
    from opentelemetry.exporter.otlp.proto.http import Compression

    return {"gzip": Compression.Gzip, "deflate": Compression.Deflate}.get(value, Compression.NoCompression)


def _grpc_compression(value: str):
    from grpc import Compression

    return {"gzip": Compression.Gzip, "deflate": Compression.Deflate}.get(value, Compression.NoCompression)


def init_telemetry(process: str = "backend") -> bool:
    """Start OpenTelemetry in this process, once; True when it is on.

    `process` names the process (`sinas.process`, and `{process}` in the
    service name): backend, queue-worker, queue-agent, queue-agent-sub,
    queue-pipeline, scheduler, cdc-worker, all-in-one. Never raises: a
    telemetry misconfiguration is logged and the process runs untraced.
    """
    global _state
    if _state is not None:
        return _state.enabled
    from app.core.telemetry_config import sdk_disabled

    if not settings.otel_enabled or sdk_disabled(os.environ):
        _state = _State()
        return False

    _state = _State(process=process)
    try:
        exporters, errors = configured_exporters()
        for error in errors:
            logger.error("OpenTelemetry exporter configuration: %s", error)
        if any(e.source == "legacy" for e in exporters):
            logger.warning(
                "OTEL_EXPORTER_ENDPOINT/OTEL_EXPORTER_HEADERS/OTEL_VENDOR are deprecated: "
                "use OTEL_EXPORTERS or the standard OTEL_EXPORTER_OTLP_* variables"
            )
        if not exporters:
            logger.warning(
                "OTEL_ENABLED is set but no exporter is configured "
                "(OTEL_EXPORTERS or OTEL_EXPORTER_OTLP_ENDPOINT); telemetry stays off"
            )
            return False
        _start(process, exporters)
        _state.enabled = _state.tracer_provider is not None or _state.logger_provider is not None
        if not _state.enabled:
            logger.warning("No OpenTelemetry exporter could be started; telemetry stays off")
            return False
        # Printed, like the other startup lines: processes don't all log INFO.
        # Names, hosts and presets only; never a header.
        print(
            f"✅ OpenTelemetry on (process={process}): "
            + "; ".join(e.describe() for e in exporters),
            flush=True,
        )
    except Exception:
        logger.exception("OpenTelemetry failed to start; continuing without it")
        _teardown()
    return _state.enabled


def _start(process: str, exporters: list) -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from app.core.telemetry_export import (
        OrphanClientSpanSampler,
        SinasAncestry,
        protect_sdk_header_warnings,
    )

    assert _state is not None
    protect_sdk_header_warnings()
    resource = build_resource(process)

    span_exporters = _built(exporters, "traces", _span_exporter)
    if span_exporters:
        provider = TracerProvider(resource=resource)
        # The standard sampler (OTEL_TRACES_SAMPLER/_ARG), minus parentless
        # client spans.
        provider.sampler = OrphanClientSpanSampler(provider.sampler)
        if any(config.spans == "sinas" for config, _ in span_exporters):
            provider.add_span_processor(SinasAncestry())
            _state.track_ancestry = True
        for _, exporter in span_exporters:
            provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _state.tracer_provider = provider
        _instrument()

    if settings.otel_logs_enabled:
        log_exporters = _built(exporters, "logs", _log_exporter)
        if log_exporters:
            _start_logs(resource, [exporter for _, exporter in log_exporters])
        else:
            logger.warning("OTEL_LOGS_ENABLED is set but no exporter has the logs signal")


def _built(exporters: list, signal: str, build: Callable[[Any], Any]) -> list[tuple[Any, Any]]:
    """(config, SDK exporter) for each exporter of `signal` that can be
    built; one that can't (gRPC without its package) is reported and left
    out, the others still export."""
    out = []
    for config in exporters:
        if signal not in config.signals:
            continue
        try:
            out.append((config, build(config)))
        except ImportError:
            logger.error(
                "OpenTelemetry exporter %s: protocol %s needs the 'otel-grpc' extra "
                "(uv sync --extra otel-grpc), which this image does not have; skipped",
                config.name,
                config.protocol if signal == "traces" else config.logs_protocol,
            )
    return out


def _start_logs(resource, log_exporters: list[Any]) -> None:
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    from app.core.telemetry_export import OTLPLogHandler, RedactingLogProcessor

    assert _state is not None
    provider = LoggerProvider(resource=resource)
    # Redaction first: processors run in order on the same record.
    provider.add_log_record_processor(RedactingLogProcessor())
    for exporter in log_exporters:
        provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    # Global, so FastAPI's own exception logs (correlated with the request
    # span) go through the same pipeline and redaction.
    set_logger_provider(provider)
    _state.logger_provider = provider

    level = logging.getLevelName(str(settings.otel_logs_level).upper())
    level = level if isinstance(level, int) else logging.INFO
    root = logging.getLogger()
    if not root.handlers:
        # Without a handler of its own, the root logger prints WARNING and
        # above through logging.lastResort; adding ours would silence that.
        root.addHandler(logging.lastResort)
    if root.level > level:
        # Let records down to `level` reach our handler, without changing
        # what the existing handlers print.
        for handler in root.handlers:
            if handler.level == logging.NOTSET:
                handler.setLevel(root.level)
        root.setLevel(level)
    handler = OTLPLogHandler(provider, level=level)
    root.addHandler(handler)
    _state.log_handler = handler


def _instrument() -> None:
    """SQLAlchemy (the platform engines), Redis, httpx and httpx2."""
    assert _state is not None
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    from app.core.database import async_engine, sync_engine

    sqlalchemy = SQLAlchemyInstrumentor()
    sqlalchemy.instrument(engines=[async_engine.sync_engine, sync_engine])
    _state.instrumented.append(sqlalchemy)

    redis = RedisInstrumentor()
    redis.instrument()
    _state.instrumented.append(redis)

    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    _state.unwrap.extend(install_outbound_policy("httpx", settings.otel_http_client_propagation))
    httpx_instrumentor = HTTPXClientInstrumentor()
    httpx_instrumentor.instrument()
    _state.instrumented.append(httpx_instrumentor)
    try:
        import httpx2  # noqa: F401
        from opentelemetry.instrumentation.httpx import HTTPX2ClientInstrumentor
    except ImportError:
        return
    _state.unwrap.extend(install_outbound_policy("httpx2", settings.otel_http_client_propagation))
    httpx2_instrumentor = HTTPX2ClientInstrumentor()
    httpx2_instrumentor.instrument()
    _state.instrumented.append(httpx2_instrumentor)


def install_outbound_policy(module_name: str, policy: str) -> list[Callable[[], None]]:
    """Limit the trace context the HTTP instrumentation adds to outgoing
    requests to what `policy` allows: `none` (default), `tracecontext`
    (traceparent, tracestate: random ids, but they tell the recipient a
    request is traced), or `all` (with baggage, which may say whom a request
    is for).

    Installed on the transport underneath the instrumentation, so it sees the
    request after the instrumentation has injected its headers. A request
    sent with HTTP instrumentation suppressed (an LLM provider's, whose
    context the provider decides) is left exactly as its sender made it.
    Returns the undo callables.
    """
    if policy == "all":
        return []
    from opentelemetry.instrumentation.utils import is_http_instrumentation_enabled, unwrap
    from opentelemetry.propagate import get_global_textmap
    from wrapt import wrap_function_wrapper

    module = __import__(module_name)
    keep = {"traceparent", "tracestate"} if policy == "tracecontext" else set()

    def _strip(wrapped, instance, args, kwargs):
        if is_http_instrumentation_enabled():
            request = args[0] if args else kwargs.get("request")
            headers = getattr(request, "headers", None)
            if headers is not None:
                fields = {"traceparent", "tracestate", "baggage"}
                fields.update(f.lower() for f in get_global_textmap().fields)
                for name in fields - keep:
                    if name in headers:
                        del headers[name]
        return wrapped(*args, **kwargs)

    wrap_function_wrapper(module_name, "HTTPTransport.handle_request", _strip)
    wrap_function_wrapper(module_name, "AsyncHTTPTransport.handle_async_request", _strip)
    return [
        lambda: unwrap(module.HTTPTransport, "handle_request"),
        lambda: unwrap(module.AsyncHTTPTransport, "handle_async_request"),
    ]


def _teardown() -> None:
    """Undo what `_start` installed (a failed start, or tests)."""
    global _state
    if _state is None:
        return
    for instrumentor in reversed(_state.instrumented):
        try:
            instrumentor.uninstrument()
        except Exception:
            pass
    for undo in reversed(_state.unwrap):
        try:
            undo()
        except Exception:
            pass
    if _state.log_handler is not None:
        logging.getLogger().removeHandler(_state.log_handler)
    for provider in (_state.tracer_provider, _state.logger_provider):
        if provider is not None:
            try:
                provider.shutdown()
            except Exception:
                pass
    _state.instrumented.clear()
    _state.unwrap.clear()
    _state.enabled = False


def flush_telemetry(timeout_millis: int = 5000) -> None:
    """Export what is buffered now (a worker shutting down). The providers
    shut down themselves at interpreter exit."""
    if not is_enabled():
        return
    assert _state is not None
    for provider in (_state.tracer_provider, _state.logger_provider):
        if provider is not None:
            try:
                provider.force_flush(timeout_millis)
            except Exception:
                pass


# ── FastAPI ─────────────────────────────────────────────────────────────────


def fastapi_telemetry_config() -> dict[str, Any]:
    """`FastAPI(telemetry=...)`: FastAPI's native OpenTelemetry (>= 0.142)
    makes the request spans, on the provider `init_telemetry` installs.

    Off unless OTEL_ENABLED, so a disabled process pays nothing per request.
    Never configures exporters of its own (`auto_configure`): an exporter
    set by FASTAPI_OTEL_AUTO_CONFIGURE would duplicate ours. No metrics:
    Sinas installs no meter provider.
    """
    if not settings.otel_enabled:
        return {"tracing": False, "metrics": False, "logs": False, "auto_configure": False}
    from app.core.telemetry_config import path_excluder

    excluded = path_excluder(
        settings.otel_excluded_paths,
        os.environ.get("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS") or os.environ.get("OTEL_PYTHON_EXCLUDED_URLS"),
    )
    return {
        "tracing": True,
        "metrics": False,
        "logs": True,
        "operation_spans": settings.otel_fastapi_operation_spans,
        "auto_configure": False,
        "exclude": lambda scope: excluded(scope.get("path", "")),
    }


# ── Errors ──────────────────────────────────────────────────────────────────


def _error_type(exc: BaseException) -> str:
    cls = type(exc)
    return cls.__qualname__ if cls.__module__ == "builtins" else f"{cls.__module__}.{cls.__qualname__}"


def record_error(span: Optional[trace.Span], exc: BaseException, description: Optional[str] = None) -> None:
    """Mark `span` failed by `exc`: an `exception` event, status ERROR,
    `error.type`. Message and traceback are redacted on export."""
    if span is None or not span.is_recording():
        return
    span.record_exception(exc)
    span.set_attribute("error.type", _error_type(exc))
    span.set_status(Status(StatusCode.ERROR, description or f"{type(exc).__name__}: {str(exc)[:200]}"))


def mark_error(span: Optional[trace.Span], description: str, error_type: str = "error") -> None:
    """Mark `span` failed without an exception (a tool that returned an error)."""
    if span is None or not span.is_recording():
        return
    span.set_attribute("error.type", error_type)
    span.set_status(Status(StatusCode.ERROR, description[:200]))


@contextmanager
def ending_on_error(span: trace.Span) -> Iterator[trace.Span]:
    """For a span started with `start_span` and ended by hand on its happy
    paths: an exception escaping the block records it, ends the span (if
    still open) and propagates."""
    try:
        yield span
    except BaseException as exc:
        if span.is_recording():
            record_error(span, exc)
            span.end()
        raise


def untraced():
    """A block whose Redis/SQL/HTTP calls are not traced: the per-chunk
    stream relay writes, which would add two spans per streamed token."""
    if not is_enabled():
        return nullcontext()
    from opentelemetry.instrumentation.utils import suppress_instrumentation

    return suppress_instrumentation()


def untraced_http():
    """HTTP client instrumentation off for the block (LLM SDK clients)."""
    if not is_enabled():
        return nullcontext()
    from opentelemetry.instrumentation.utils import suppress_http_instrumentation

    return suppress_http_instrumentation()


# ── Context propagation (queue) ─────────────────────────────────────────────


def inject_trace_context() -> dict[str, str]:
    """The current context as W3C fields (`traceparent`, `baggage`...)."""
    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier


def inject_job_context() -> dict[str, str]:
    """`inject_trace_context` for a queued job: also says which span the
    job's spans hang under for an exporter of Sinas's own spans only. Never
    sent over HTTP."""
    carrier = inject_trace_context()
    if is_enabled() and _state is not None and _state.track_ancestry and carrier:
        from app.core.telemetry_export import JOB_ANCESTOR_FIELD, job_ancestor

        ancestor = job_ancestor()
        if ancestor is not None:
            carrier[JOB_ANCESTOR_FIELD] = ancestor
    return carrier


def extract_trace_context(carrier: dict[str, str]) -> Optional[otel_context.Context]:
    """Deserialize trace context from a dict (received from queue kwargs)."""
    if not carrier:
        return None
    ctx = extract(carrier=carrier)
    if "sinas-ancestor" in carrier and is_enabled():
        from app.core.telemetry_export import with_job_ancestor

        ctx = with_job_ancestor(ctx, carrier["sinas-ancestor"])
    return ctx


@contextmanager
def attached(ctx: Optional[otel_context.Context]) -> Iterator[None]:
    """Make `ctx` the current context for the block, then restore the previous one.

    Starting a span with `context=ctx` only takes its parent from `ctx`; the
    baggage `ctx` carries is not current inside that span. Attaching makes the
    whole of it current — the trace and the baggage — so a provider call made
    in the block can propagate both (`inject_trace_context`). `None` attaches
    nothing.
    """
    if ctx is None:
        yield
        return
    token = otel_context.attach(ctx)
    try:
        yield
    finally:
        otel_context.detach(token)


def traced(span_name: str, kind: trace.SpanKind = trace.SpanKind.INTERNAL) -> Callable[[F], F]:
    """An async function run as span `span_name` (current for its body,
    exceptions recorded): for an entry point that starts work on its own,
    such as a scheduled run firing."""

    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        async def run(*args: Any, **kwargs: Any) -> Any:
            with get_tracer().start_as_current_span(
                span_name, kind=kind, attributes={"code.function.name": fn.__qualname__}
            ):
                return await fn(*args, **kwargs)

        return run  # type: ignore[return-value]

    return decorate


def traced_job(
    span_name: str, failed: Optional[Callable[[Any], Optional[str]]] = None
) -> Callable[[F], F]:
    """An arq job run as a CONSUMER span `span_name`, in the trace context it
    was enqueued with (`trace_context` kwarg). The span is marked failed when
    the job raises (not for `arq.Retry`, which is flow control), is cancelled
    (a worker timeout or shutdown), or returns what `failed` says is a
    failure (it returns the description, or None)."""

    def decorate(job: F) -> F:
        @functools.wraps(job)
        async def run(ctx: dict, *args: Any, **kwargs: Any) -> Any:
            parent = extract_trace_context(kwargs.get("trace_context") or {})
            with attached(parent), get_tracer().start_as_current_span(
                span_name,
                kind=trace.SpanKind.CONSUMER,
                attributes={
                    "messaging.system": "arq",
                    "messaging.operation.type": "process",
                    "job.function": job.__name__,
                    **({"job.id": str(ctx["job_id"])} if isinstance(ctx, dict) and ctx.get("job_id") else {}),
                },
                record_exception=False,
                set_status_on_exception=False,
            ) as span:
                try:
                    result = await job(ctx, *args, **kwargs)
                except asyncio.CancelledError:
                    mark_error(span, "Job cancelled or timed out", "cancelled")
                    raise
                except Exception as exc:
                    if type(exc).__name__ != "Retry":
                        record_error(span, exc)
                    raise
                if failed is not None:
                    try:
                        description = failed(result)
                    except Exception:
                        description = None
                    if description:
                        mark_error(span, description, "job_failed")
                return result

        return run  # type: ignore[return-value]

    return decorate


