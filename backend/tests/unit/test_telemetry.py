"""OpenTelemetry: configuration, the export pipeline, instrumentation, errors, logs.

Pinned here:
- configuration: the standard OTEL_EXPORTER_OTLP_* variables, a list of
  named exporters (OTEL_EXPORTERS, with per-name env overrides) and the
  deprecated single exporter, alone or all at once; bad entries reported
  without their header values;
- disabled means nothing installed: no provider, no instrumentation, FastAPI
  tracing off;
- each exporter gets Sinas's LLM attributes in its own preset, URLs and
  exception text redacted, and (scope "sinas") only Sinas's own spans,
  re-parented onto each other;
- exceptions mark spans ERROR: requests, jobs, tools, LLM calls;
- an LLM provider's request carries no trace context unless the provider
  is configured to propagate it, and gets no duplicate HTTP client span,
  with the HTTP instrumentation installed; other outgoing requests follow
  OTEL_HTTP_CLIENT_PROPAGATION;
- header secrets never reach a log line; exported logs are redacted.

No network: exporters are in memory, HTTP goes to in-process fakes.
"""
import asyncio
import json
import logging

import httpcore
import httpx
import openai
import pytest
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from opentelemetry.util._once import Once
from starlette.testclient import TestClient

from app.core import telemetry
from app.core import telemetry_export as tx
from app.core.config import settings
from app.core.telemetry_config import (
    ExporterConfig,
    legacy_exporter,
    list_exporters,
    parse_headers,
    path_excluder,
    resolve_exporters,
    safe_endpoint,
    standard_exporter,
)

SECRET = "sk-lf-THIS-IS-A-SECRET-0123456789"
TRACE_ID = "4c79f60c11214eb38604f4ae0781bfb2"
TRACEPARENT = f"00-{TRACE_ID}-b2a7c1d9e8f30411-01"


# ── fixtures ────────────────────────────────────────────────────────────────


def _reset_otel_globals() -> None:
    from opentelemetry._logs import _internal as logs_internal

    trace._TRACER_PROVIDER_SET_ONCE = Once()
    trace._TRACER_PROVIDER = None
    logs_internal._LOGGER_PROVIDER_SET_ONCE = Once()
    logs_internal._LOGGER_PROVIDER = None


@pytest.fixture
def fresh_telemetry(monkeypatch):
    """A process whose telemetry has not started; undone afterwards."""
    monkeypatch.setattr(telemetry, "_state", None)
    _reset_otel_globals()
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_SDK_DISABLED"):
        monkeypatch.delenv(name, raising=False)
    yield
    telemetry._teardown()
    _reset_otel_globals()


@pytest.fixture
def memory(fresh_telemetry, monkeypatch):
    """Telemetry started from settings, every exporter writing to memory.

    Returns {exporter name: InMemorySpanExporter}.
    """
    sinks: dict[str, InMemorySpanExporter] = {}

    def span_exporter(config):
        sinks[config.name] = InMemorySpanExporter()
        return tx.SinasSpanExporter(sinks[config.name], preset=config.preset, spans=config.spans)

    monkeypatch.setattr(telemetry, "_span_exporter", span_exporter)

    from opentelemetry.sdk.trace import export as sdk_export

    # Synchronous export, so a test sees a span as soon as it ends.
    monkeypatch.setattr(sdk_export, "BatchSpanProcessor", SimpleSpanProcessor)
    monkeypatch.setattr(settings, "otel_enabled", True)
    monkeypatch.setattr(
        settings,
        "otel_exporters",
        json.dumps([
            {"name": "apm", "endpoint": "http://collector.test:4318", "preset": "none"},
            {"name": "llm", "endpoint": "https://llm.test/otel", "preset": "langfuse",
             "headers": {"authorization": f"Basic {SECRET}"}},
        ]),
    )
    monkeypatch.setattr(settings, "otel_exporter_endpoint", None)
    assert telemetry.init_telemetry("backend") is True
    return sinks


@pytest.fixture
def spans(fresh_telemetry):
    """A plain in-memory provider, for code that records spans."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter
    provider.shutdown()


# ── configuration ───────────────────────────────────────────────────────────


def test_standard_variables_make_one_exporter_the_sdk_configures():
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}
    config = standard_exporter(env, preset="genai")
    assert config.name == "otlp" and config.source == "standard"
    assert config.traces_endpoint is None and config.headers is None  # the SDK reads them
    assert config.signals == {"traces", "logs"}
    assert config.protocol == "http/protobuf" and config.spans == "all"

    grpc = standard_exporter({**env, "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}, preset="none")
    assert grpc.protocol == "grpc" and grpc.preset == "none"

    traces_only = standard_exporter(
        {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://c:4318/v1/traces"}, preset="genai"
    )
    assert traces_only.signals == {"traces"}

    assert standard_exporter({}, preset="genai") is None  # never the SDK's localhost default
    assert standard_exporter({**env, "OTEL_TRACES_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none"}, preset="genai") is None


def test_a_list_of_exporters_each_with_its_own_settings():
    raw = json.dumps([
        {"name": "langfuse", "endpoint": "https://cloud.langfuse.com/api/public/otel", "preset": "langfuse",
         "headers": "x-langfuse-ingestion-version=4"},
        {"name": "sentry", "endpoint": "https://o1.ingest.sentry.io/api/2/integration/otlp/v1/traces",
         "preset": "none", "signals": ["traces", "logs"]},
        {"name": "tempo", "endpoint": "otel-collector:4317", "protocol": "grpc", "insecure": True},
    ])
    env = {
        "OTEL_EXPORTER_LANGFUSE_HEADERS": f"Authorization=Basic%20{SECRET}",
        "OTEL_EXPORTER_SENTRY_HEADERS": f"x-sentry-auth=sentry sentry_key={SECRET}",
    }
    exporters, errors = list_exporters(raw, env)
    assert errors == []
    langfuse, sentry, tempo = exporters

    assert langfuse.traces_endpoint == "https://cloud.langfuse.com/api/public/otel/v1/traces"
    # JSON headers and the secret from the per-name variable, merged.
    assert langfuse.headers == {"x-langfuse-ingestion-version": "4", "authorization": f"Basic {SECRET}"}
    assert langfuse.spans == "sinas"  # an LLM tool gets Sinas's own spans by default

    # A full signal URL, as some vendors print it, keeps its path; logs beside it.
    assert sentry.traces_endpoint.endswith("/integration/otlp/v1/traces")
    assert sentry.logs_endpoint.endswith("/integration/otlp/v1/logs")
    assert sentry.headers == {"x-sentry-auth": f"sentry sentry_key={SECRET}"}
    assert sentry.signals == {"traces", "logs"} and sentry.spans == "all"

    assert tempo.protocol == "grpc" and tempo.traces_endpoint == "otel-collector:4317"
    assert tempo.preset == "genai" and tempo.insecure is True


def test_an_endpoint_query_is_kept_where_it_is():
    raw = json.dumps([
        {"name": "a", "endpoint": f"https://c.test/otel/v1/traces?key={SECRET}"},
        {"name": "b", "endpoint": f"https://c.test/otel?key={SECRET}"},
    ])
    a, b = list_exporters(raw, {})[0]
    assert a.traces_endpoint == f"https://c.test/otel/v1/traces?key={SECRET}"
    assert a.logs_endpoint == f"https://c.test/otel/v1/logs?key={SECRET}"
    assert b.traces_endpoint == f"https://c.test/otel/v1/traces?key={SECRET}"


def test_a_per_name_variable_overrides_the_endpoint_and_preset():
    raw = json.dumps([{"name": "my-apm", "endpoint": "http://a:4318"}])
    env = {"OTEL_EXPORTER_MY_APM_ENDPOINT": "http://b:4318", "OTEL_EXPORTER_MY_APM_PRESET": "none"}
    (config,), errors = list_exporters(raw, env)
    assert not errors
    assert config.traces_endpoint == "http://b:4318/v1/traces" and config.preset == "none"


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "otlp", "endpoint": "http://a"},
        {"name": "legacy", "endpoint": "http://a"},
        {"name": "Bad Name", "endpoint": "http://a"},
        {"name": "x"},
        {"name": "x", "endpoint": "ftp://a"},
        {"name": "x", "endpoint": "http://a", "protocol": "http/json"},
        {"name": "x", "endpoint": "http://a", "preset": "datadog"},
        {"name": "x", "endpoint": "http://a", "signals": ["metrics"]},
        {"name": "x", "endpoint": "http://a", "headers": {"authorization": 42.5, "x": [SECRET]}},
        {"name": "x", "endpoint": "http://a", "headers": f"{SECRET}"},
        {"name": "x", "endpoint": "http://a", "traces_endpoint": 123},
        {"name": "x", "endpoint": ["http://a"]},
    ],
)
def test_a_bad_entry_is_reported_without_its_secrets_and_skipped(entry):
    raw = json.dumps([entry, {"name": "good", "endpoint": "http://ok:4318"}])
    exporters, errors = list_exporters(raw, {})
    assert [e.name for e in exporters] == ["good"]
    assert len(errors) == 1
    assert SECRET not in errors[0]


def test_duplicate_names_and_invalid_json_are_reported():
    _, errors = list_exporters(json.dumps([{"name": "a", "endpoint": "http://a"}] * 2), {})
    assert "used twice" in errors[0]
    _, errors = list_exporters('[{"name": "a", "headers": {"k": "' + SECRET + '"}', {})
    assert "not valid JSON" in errors[0] and SECRET not in errors[0]


def test_the_deprecated_single_exporter_still_works_as_before():
    config, errors = legacy_exporter(
        "https://app.langwatch.ai/api/otel/v1/traces", f"X-Auth-Token={SECRET},X-Other=a=b", None
    )
    assert not errors
    assert config.name == "legacy" and config.preset == "langwatch"  # OTEL_VENDOR's default
    assert config.traces_endpoint == "https://app.langwatch.ai/api/otel/v1/traces"
    assert config.headers == {"X-Auth-Token": SECRET, "X-Other": "a=b"}  # parsed as it always was
    assert config.spans == "sinas" and config.signals == {"traces"}

    assert legacy_exporter("https://x/v1/traces", None, "langfuse")[0].preset == "langfuse"
    assert legacy_exporter(None, None, None) == (None, [])


def test_every_source_at_once():
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}
    exporters, errors = resolve_exporters(
        env,
        exporters_json=json.dumps([{"name": "langfuse", "endpoint": "https://lf/otel", "preset": "langfuse"}]),
        standard_preset="none",
        legacy_endpoint="https://lw/v1/traces",
        legacy_vendor="langwatch",
    )
    assert not errors
    assert [(e.name, e.preset) for e in exporters] == [
        ("otlp", "none"), ("langfuse", "langfuse"), ("legacy", "langwatch"),
    ]
    assert resolve_exporters({**env, "OTEL_SDK_DISABLED": "true"}, exporters_json=None, standard_preset="genai") == ([], [])


def test_a_config_never_shows_its_headers():
    config = ExporterConfig(
        name="x", source="list", traces_endpoint=f"https://user:{SECRET}@host/v1/traces?key={SECRET}",
        headers={"authorization": SECRET},
    )
    assert SECRET not in repr(config)
    assert SECRET not in config.describe()
    assert safe_endpoint(f"https://user:{SECRET}@host/p?k={SECRET}") == "https://host/p"


def test_header_strings_in_the_otel_format():
    assert parse_headers("a=1, b = x=y ,c=Basic%20abc%3D", where="t") == {"a": "1", "b": "x=y", "c": "Basic abc="}
    assert parse_headers({"Authorization": "Bearer t"}, where="t") == {"authorization": "Bearer t"}


def test_excluded_request_paths():
    excluded = path_excluder("/health,/api/v1/system/health,/ui/*", r"^/metrics")
    assert excluded("/health") and excluded("/health/")
    assert excluded("/api/v1/system/health")
    assert excluded("/ui/assets/app.js")
    assert excluded("/metrics/prom")
    assert not excluded("/api/v1/agents") and not excluded("/healthy")


# ── disabled ────────────────────────────────────────────────────────────────


def test_disabled_installs_nothing(fresh_telemetry, monkeypatch):
    monkeypatch.setattr(settings, "otel_enabled", False)
    monkeypatch.setattr(settings, "otel_exporters", json.dumps([{"name": "a", "endpoint": "http://a"}]))
    assert telemetry.init_telemetry("backend") is False
    assert not telemetry.is_enabled()

    assert type(trace.get_tracer_provider()).__name__ == "ProxyTracerProvider"
    assert not hasattr(httpx.AsyncHTTPTransport.handle_async_request, "__wrapped__")
    from redis.asyncio import Redis

    assert not hasattr(Redis.execute_command, "__wrapped__")
    assert telemetry.fastapi_telemetry_config()["tracing"] is False
    assert not trace.get_tracer("sinas").start_span("x").is_recording()
    # The helpers are free no-ops.
    assert telemetry.inject_job_context() == {}
    with telemetry.untraced(), telemetry.untraced_http():
        pass


def test_enabled_without_an_exporter_stays_off(fresh_telemetry, monkeypatch, caplog):
    monkeypatch.setattr(settings, "otel_enabled", True)
    monkeypatch.setattr(settings, "otel_exporters", None)
    monkeypatch.setattr(settings, "otel_exporter_endpoint", None)
    with caplog.at_level(logging.WARNING):
        assert telemetry.init_telemetry("backend") is False
    assert "no exporter" in caplog.text
    assert not hasattr(httpx.AsyncHTTPTransport.handle_async_request, "__wrapped__")


def test_sdk_disabled_wins(fresh_telemetry, monkeypatch):
    monkeypatch.setattr(settings, "otel_enabled", True)
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setattr(settings, "otel_exporters", json.dumps([{"name": "a", "endpoint": "http://a"}]))
    assert telemetry.init_telemetry("backend") is False


# ── enabled: resource, instrumentation, export ──────────────────────────────


def test_enabled_installs_instrumentation_and_undoes_it(memory):
    assert telemetry.is_enabled()
    assert hasattr(httpx.AsyncHTTPTransport.handle_async_request, "__wrapped__")
    telemetry._teardown()
    assert not hasattr(httpx.AsyncHTTPTransport.handle_async_request, "__wrapped__")


def test_the_resource_names_the_process(fresh_telemetry, monkeypatch):
    monkeypatch.setattr(settings, "otel_service_name", None)
    resource = telemetry.build_resource("queue-agent")
    assert resource.attributes["service.name"] == "sinas"
    assert resource.attributes["sinas.process"] == "queue-agent"

    monkeypatch.setattr(settings, "otel_service_name", "sinas-{process}")
    assert telemetry.build_resource("scheduler").attributes["service.name"] == "sinas-scheduler"

    monkeypatch.setattr(settings, "otel_service_name", None)
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.name=platform,deployment.environment=prod")
    resource = telemetry.build_resource("backend")
    assert resource.attributes["service.name"] == "platform"
    assert resource.attributes["deployment.environment"] == "prod"


def _llm_span(name: str = "llm.call", **extra) -> None:
    tracer = telemetry.get_tracer()
    with tracer.start_as_current_span(name, attributes={
        "gen_ai.request.model": "m",
        "gen_ai.system": "openai",
        telemetry.otel_attr("span_type"): "llm",
        telemetry.otel_attr("input"): "what is 2+2?",
        telemetry.otel_attr("output"): "4",
        telemetry.otel_attr("thread_id"): "chat-1",
        telemetry.otel_attr("user_id"): "user-1",
        telemetry.otel_attr("labels"): json.dumps(["agent:default/a"]),
        **extra,
    }):
        pass


def test_each_exporter_gets_its_own_attribute_preset(memory):
    _llm_span(**{"function.result": "secret result"})
    (apm,) = memory["apm"].get_finished_spans()
    (llm,) = memory["llm"].get_finished_spans()

    # An APM gets no prompt/response content, ids yes.
    assert "sinas.input" not in apm.attributes and "sinas.output" not in apm.attributes
    assert "function.result" not in apm.attributes
    assert not any("input" in k or "output" in k for k in apm.attributes)
    assert apm.attributes["gen_ai.conversation.id"] == "chat-1"
    assert apm.attributes["gen_ai.request.model"] == "m"

    # Langfuse gets its own names.
    assert llm.attributes["langfuse.observation.input"] == "what is 2+2?"
    assert llm.attributes["langfuse.observation.output"] == "4"
    assert llm.attributes["langfuse.observation.type"] == "generation"
    assert llm.attributes["langfuse.session.id"] == "chat-1"
    assert llm.attributes["langfuse.user.id"] == "user-1"
    assert tuple(llm.attributes["langfuse.trace.tags"]) == ("agent:default/a",)
    # Internal bookkeeping never leaves the process.
    assert tx.ANCESTOR_ATTR not in apm.attributes and tx.ANCESTOR_ATTR not in llm.attributes


def _roles_through(preset: str, span_type: str = "llm") -> dict:
    sink = InMemorySpanExporter()
    exporter = tx.SinasSpanExporter(sink, preset=preset)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("sinas").start_as_current_span("s", attributes={
        "sinas.span.type": span_type, "sinas.input": "in", "sinas.output": "out",
        "gen_ai.conversation.id": "c", "user.id": "u", "sinas.labels": '["l"]', "gen_ai.system": "openai",
    }):
        pass
    return dict(sink.get_finished_spans()[0].attributes)


def test_the_legacy_presets_keep_their_names():
    assert _roles_through("langwatch") == {
        "langwatch.span.type": "llm", "langwatch.input": "in", "langwatch.output": "out",
        "langwatch.thread.id": "c", "langwatch.user.id": "u", "langwatch.labels": '["l"]',
        "gen_ai.system": "openai",
    }
    generic = _roles_through("generic_otel")
    assert generic["gen_ai.thread.id"] == "c" and generic["gen_ai.input"] == "in"


def test_the_genai_preset_follows_the_semantic_conventions():
    attrs = _roles_through("genai")
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.provider.name"] == "openai"
    assert json.loads(attrs["gen_ai.input.messages"])[0]["parts"][0]["content"] == "in"
    assert json.loads(attrs["gen_ai.output.messages"])[0]["role"] == "assistant"
    tool = _roles_through("genai", span_type="tool")
    assert tool["gen_ai.tool.call.arguments"] == "in" and tool["gen_ai.tool.call.result"] == "out"
    assert tool["gen_ai.operation.name"] == "execute_tool"


def test_urls_and_exception_text_are_redacted_on_export():
    sink = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(tx.SinasSpanExporter(sink, preset="none")))
    with provider.get_tracer("x").start_as_current_span("GET", attributes={
        "url.full": f"https://user:pw@api.test/watch/{SECRET}?token={SECRET}&page=2",
        "url.query": f"api_key={SECRET}&q=1",
        "url.path": f"/watch/{SECRET}",
    }) as span:
        try:
            raise ValueError(f"connect failed: postgresql://app:{SECRET}@db/x Authorization: Bearer {SECRET}")
        except ValueError as e:
            telemetry.record_error(span, e)
    (out,) = sink.get_finished_spans()
    blob = json.dumps({k: str(v) for k, v in out.attributes.items()}) + str(
        [dict(e.attributes) for e in out.events]
    ) + str(out.status.description)
    assert SECRET not in blob
    assert out.attributes["url.query"].endswith("&q=1")
    assert "page=2" in out.attributes["url.full"]
    assert out.status.status_code == StatusCode.ERROR


def test_an_llm_tool_only_gets_sinas_spans_parented_onto_each_other(memory):
    tracer = telemetry.get_tracer()
    server = trace.get_tracer("fastapi").start_span("POST /chats", kind=SpanKind.SERVER)
    with trace.use_span(server, end_on_exit=True):
        with tracer.start_as_current_span("conversation.turn") as turn:
            db = trace.get_tracer("sqlalchemy").start_span("SELECT", kind=SpanKind.CLIENT)
            with trace.use_span(db, end_on_exit=True):
                _llm_span()

    llm_names = [s.name for s in memory["llm"].get_finished_spans()]
    assert llm_names == ["llm.call", "conversation.turn"]
    by_name = {s.name: s for s in memory["llm"].get_finished_spans()}
    # The LLM call hangs under the turn, across the (unexported) DB span...
    assert by_name["llm.call"].parent.span_id == turn.get_span_context().span_id
    # ...and the turn is a root there: its request span isn't exported.
    assert by_name["conversation.turn"].parent is None

    # The APM sees everything, as recorded.
    apm = {s.name: s for s in memory["apm"].get_finished_spans()}
    assert set(apm) == {"POST /chats", "conversation.turn", "SELECT", "llm.call"}
    assert apm["llm.call"].parent.span_id == apm["SELECT"].context.span_id


def test_a_queued_job_hangs_under_the_sinas_span_that_enqueued_it(memory):
    tracer = telemetry.get_tracer()
    server = trace.get_tracer("fastapi").start_span("POST /x", kind=SpanKind.SERVER)
    with trace.use_span(server, end_on_exit=True):
        with tracer.start_as_current_span("agent.delegate") as delegate:
            redis = trace.get_tracer("redis").start_span("EVALSHA", kind=SpanKind.CLIENT)
            with trace.use_span(redis, end_on_exit=True):
                carrier = telemetry.inject_job_context()
    assert carrier["sinas-ancestor"] == format(delegate.get_span_context().span_id, "016x")

    # In the worker: the job's span is parented on the delegate span for the
    # LLM tool, on the true (Redis) parent for the APM.
    with telemetry.attached(telemetry.extract_trace_context(carrier)):
        with tracer.start_as_current_span("agent.job"):
            pass
    job_llm = [s for s in memory["llm"].get_finished_spans() if s.name == "agent.job"][0]
    job_apm = [s for s in memory["apm"].get_finished_spans() if s.name == "agent.job"][0]
    assert job_llm.parent.span_id == delegate.get_span_context().span_id
    assert job_apm.parent.span_id == redis.get_span_context().span_id


def test_a_parentless_client_span_is_dropped(memory):
    trace.get_tracer("redis").start_span("BRPOP", kind=SpanKind.CLIENT).end()
    with telemetry.get_tracer().start_as_current_span("job"):
        trace.get_tracer("redis").start_span("GET", kind=SpanKind.CLIENT).end()
    assert [s.name for s in memory["apm"].get_finished_spans()] == ["GET", "job"]


# ── errors ──────────────────────────────────────────────────────────────────


def _error_span(exporter, name):
    (span,) = [s for s in exporter.get_finished_spans() if s.name == name]
    assert span.status.status_code == StatusCode.ERROR
    return span


def test_a_failing_request_marks_its_span_error(memory, monkeypatch):
    monkeypatch.setattr(settings, "otel_excluded_paths", "/health")
    app = FastAPI(telemetry=telemetry.fastapi_telemetry_config())

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    @app.get("/health")
    async def health():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/boom").status_code == 500
    assert client.get("/health").status_code == 200

    span = _error_span(memory["apm"], "GET /boom")
    assert span.kind == SpanKind.SERVER
    assert span.attributes["error.type"] == "RuntimeError"
    assert not [s for s in memory["apm"].get_finished_spans() if "health" in s.name]


def test_a_request_span_is_a_child_of_the_callers_and_current_in_the_endpoint(memory):
    from app.middleware.trace_context import TraceContextMiddleware

    app = FastAPI(telemetry=telemetry.fastapi_telemetry_config())
    app.add_middleware(TraceContextMiddleware)
    seen = {}

    @app.get("/x")
    async def x():
        seen["span"] = trace.get_current_span().get_span_context()
        return {}

    TestClient(app).get("/x", headers={"traceparent": TRACEPARENT})
    (server,) = [s for s in memory["apm"].get_finished_spans() if s.kind == SpanKind.SERVER]
    assert format(server.context.trace_id, "032x") == TRACE_ID
    assert server.parent.span_id == int("b2a7c1d9e8f30411", 16)
    # The endpoint runs inside the request span (or one of FastAPI's operation
    # spans under it), not directly inside the caller's.
    assert seen["span"].span_id != int("b2a7c1d9e8f30411", 16)
    assert seen["span"].trace_id == server.context.trace_id


def test_record_error(spans):
    with trace.get_tracer("t").start_as_current_span("s") as span:
        telemetry.record_error(span, KeyError("k"))
    (out,) = spans.get_finished_spans()
    assert out.status.status_code == StatusCode.ERROR
    assert out.attributes["error.type"] == "KeyError"
    assert out.events[0].name == "exception"


def test_ending_on_error_ends_an_open_span(spans):
    span = trace.get_tracer("t").start_span("llm.call")
    with pytest.raises(TimeoutError):
        with telemetry.ending_on_error(span):
            raise TimeoutError("provider timed out")
    _error_span(spans, "llm.call")


def test_a_failing_job_marks_its_span_error_retry_does_not(spans):
    from arq.worker import Retry

    @telemetry.traced_job("pipeline.run")
    async def failing(ctx, **kwargs):
        raise ValueError("bad input")

    @telemetry.traced_job("pipeline.fire")
    async def retrying(ctx, **kwargs):
        raise Retry(defer=1)

    with pytest.raises(ValueError):
        asyncio.run(failing({"job_id": "j1"}, trace_context={"traceparent": TRACEPARENT}))
    with pytest.raises(Retry):
        asyncio.run(retrying({}))

    failed = _error_span(spans, "pipeline.run")
    assert failed.kind == SpanKind.CONSUMER
    assert format(failed.context.trace_id, "032x") == TRACE_ID  # in the enqueued context
    assert failed.attributes["job.id"] == "j1"
    (retried,) = [s for s in spans.get_finished_spans() if s.name == "pipeline.fire"]
    assert retried.status.status_code != StatusCode.ERROR


def test_a_cancelled_job_and_a_returned_failure_mark_the_span(spans):
    @telemetry.traced_job("code.background")
    async def cancelled(ctx, **kwargs):
        raise asyncio.CancelledError()

    @telemetry.traced_job("pipeline.run", failed=lambda r: r.get("error") if r["status"] == "failed" else None)
    async def returns(ctx, **kwargs):
        return kwargs["result"]

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await cancelled({})
        await returns({}, result={"status": "failed", "error": "step 2 failed"})
        await returns({}, result={"status": "succeeded"})

    asyncio.run(run())
    cancelled_span = _error_span(spans, "code.background")
    assert cancelled_span.attributes["error.type"] == "cancelled"
    failed_run, ok_run = [s for s in spans.get_finished_spans() if s.name == "pipeline.run"]
    assert failed_run.status.status_code == StatusCode.ERROR
    assert failed_run.status.description == "step 2 failed"
    assert ok_run.status.status_code != StatusCode.ERROR


def test_a_tool_that_fails_marks_its_span_error(spans, monkeypatch):
    from app.services import tool_execution

    async def raising(*args, **kwargs):
        raise RuntimeError("tool exploded")

    async def returning_error(*args, **kwargs):
        return ("c2", "t", json.dumps({"error": "not allowed"}))

    call = {"id": "c1", "function": {"name": "t", "arguments": "{}"}}
    monkeypatch.setattr(tool_execution, "_execute_single_tool", raising)
    with pytest.raises(RuntimeError):
        asyncio.run(tool_execution.execute_single_tool(call, "chat", "user", "tok", []))
    monkeypatch.setattr(tool_execution, "_execute_single_tool", returning_error)
    asyncio.run(tool_execution.execute_single_tool(call, "chat", "user", "tok", []))

    async def delegation_failed(*args, **kwargs):  # the error is not the first key
        return ("c3", "t", json.dumps({"agent_name": "x", "error": "Sub-agent failed", "chat_id": "c"}))

    async def fine(*args, **kwargs):
        return ("c4", "t", json.dumps({"result": "ok", "errors_seen": 0}))

    for impl in (delegation_failed, fine):
        monkeypatch.setattr(tool_execution, "_execute_single_tool", impl)
        asyncio.run(tool_execution.execute_single_tool(call, "chat", "user", "tok", []))

    raised, returned, delegated, ok = [s for s in spans.get_finished_spans() if s.name == "tool.call"]
    assert raised.status.status_code == StatusCode.ERROR and raised.events[0].name == "exception"
    assert returned.status.status_code == StatusCode.ERROR
    assert returned.status.description == "not allowed"
    assert returned.attributes["gen_ai.tool.name"] == "t"
    assert delegated.status.description == "Sub-agent failed"
    assert ok.status.status_code != StatusCode.ERROR


def test_a_failing_streamed_llm_call_is_recorded(spans):
    from app.services import message_service

    message_service._record_llm_span(1, {"sinas.span.type": "llm"}, error=ConnectionError("reset"))
    span = _error_span(spans, "llm.call")
    assert span.start_time == 1


# ── outgoing HTTP: LLM providers vs everything else ─────────────────────────


class _Pool:
    """Stands for the connection pool under a real (instrumented) transport."""

    def __init__(self, status: int = 400) -> None:
        self.sent: list[dict[str, str]] = []
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def handle_async_request(self, request):
        self.sent.append({k.decode().lower(): v.decode() for k, v in request.headers})
        body = json.dumps({"error": {"message": "refused by the test"}}).encode()
        return httpcore.Response(self.status, headers=[(b"content-type", b"application/json")], content=body)


def _client(pool: _Pool, **kwargs) -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport()
    transport._pool = pool
    return httpx.AsyncClient(transport=transport, **kwargs)


def _openai_provider(pool: _Pool, propagate: bool):
    from app.providers import OpenAIProvider
    from app.providers.openai_provider import openai_client

    provider = OpenAIProvider(api_key="k", base_url="http://gateway.test/v1")
    provider.client = openai_client(
        api_key="k", base_url="http://gateway.test/v1", http_client=_client(pool), max_retries=0
    )
    provider.propagate_context = propagate
    return provider


def _inside_a_request(coro_factory) -> None:
    async def run():
        ctx = telemetry.extract_trace_context({"traceparent": TRACEPARENT, "baggage": "org_id=org-9"})
        with telemetry.attached(ctx), telemetry.get_tracer().start_as_current_span("llm.call"):
            await coro_factory()

    asyncio.run(run())


@pytest.mark.parametrize("propagate", [False, True])
def test_an_llm_call_carries_context_only_when_configured_and_gets_no_http_span(memory, propagate):
    pool = _Pool()
    provider = _openai_provider(pool, propagate)

    async def call():
        with pytest.raises(openai.BadRequestError):
            await provider.complete([{"role": "user", "content": "hi"}], model="m", max_tokens=1)

    _inside_a_request(call)
    (headers,) = pool.sent
    if propagate:
        assert headers["traceparent"].split("-")[1] == TRACE_ID
        assert headers["baggage"] == "org_id=org-9"
    else:
        assert "traceparent" not in headers and "baggage" not in headers
    assert "x-sinas-propagate-context" not in headers
    # Recorded once, as Sinas's llm.call span: no HTTP client span beside it.
    assert not [s for s in memory["apm"].get_finished_spans() if s.kind == SpanKind.CLIENT]


@pytest.mark.parametrize(
    "policy,expected",
    [
        ("none", set()),
        ("tracecontext", {"traceparent"}),
        ("all", {"traceparent", "baggage"}),
    ],
)
def test_other_outgoing_calls_are_traced_and_propagate_per_policy(fresh_telemetry, monkeypatch, policy, expected):
    monkeypatch.setattr(settings, "otel_http_client_propagation", policy)
    sink = InMemorySpanExporter()
    monkeypatch.setattr(telemetry, "_span_exporter", lambda c: tx.SinasSpanExporter(sink, preset=c.preset))
    from opentelemetry.sdk.trace import export as sdk_export

    monkeypatch.setattr(sdk_export, "BatchSpanProcessor", SimpleSpanProcessor)
    monkeypatch.setattr(settings, "otel_enabled", True)
    monkeypatch.setattr(settings, "otel_exporters", json.dumps([{"name": "apm", "endpoint": "http://c:4318"}]))
    monkeypatch.setattr(settings, "otel_exporter_endpoint", None)
    assert telemetry.init_telemetry("backend")

    pool = _Pool(status=200)

    async def call():
        async with _client(pool) as client:
            await client.post(f"https://hooks.test/in?token={SECRET}", json={})

    _inside_a_request(call)
    (headers,) = pool.sent
    assert {k for k in ("traceparent", "tracestate", "baggage") if k in headers} == expected
    (client_span,) = [s for s in sink.get_finished_spans() if s.kind == SpanKind.CLIENT]
    assert SECRET not in str(dict(client_span.attributes))


# ── secrets and logs ────────────────────────────────────────────────────────


def test_header_secrets_never_reach_a_log_line(fresh_telemetry, monkeypatch, caplog, capsys):
    monkeypatch.setattr(settings, "otel_enabled", True)
    monkeypatch.setenv("OTEL_EXPORTER_GOOD_HEADERS", f"authorization=Bearer {SECRET}")
    monkeypatch.setattr(
        settings,
        "otel_exporters",
        json.dumps([
            {"name": "good", "endpoint": f"https://user:{SECRET}@otel.test/v1/traces?k={SECRET}"},
            {"name": "bad", "endpoint": "http://b", "headers": {"x-key": [SECRET]}},
        ]),
    )
    monkeypatch.setattr(settings, "otel_exporter_endpoint", "https://lw.test/v1/traces")
    monkeypatch.setattr(settings, "otel_exporter_headers", f"X-Auth-Token={SECRET}")
    with caplog.at_level(logging.DEBUG):
        assert telemetry.init_telemetry("backend")
        # The SDK's own header parser, on a malformed pair, logs it verbatim
        # unless filtered.
        from opentelemetry.util.re import parse_env_headers

        parse_env_headers(f"bad header {SECRET}", liberal=True)
    printed = capsys.readouterr()
    # The startup line names each exporter and its host...
    assert "good (https://otel.test/v1/traces" in printed.out
    assert "OTEL_EXPORTERS[bad]" in caplog.text  # ...and errors name the entry...
    # ...never a credential.
    assert SECRET not in caplog.text + printed.out + printed.err


def test_exported_logs_are_redacted_and_correlated(fresh_telemetry, monkeypatch):
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import InMemoryLogExporter, SimpleLogRecordProcessor

    exporter = InMemoryLogExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(tx.RedactingLogProcessor())
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = tx.OTLPLogHandler(provider, level=logging.INFO)
    log = logging.getLogger("app.test_otel_logs")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    tracer_provider = TracerProvider()
    try:
        with tracer_provider.get_tracer("t").start_as_current_span("job") as span:
            log.info("calling https://api.test/x?api_key=%s with password=%s", SECRET, SECRET, extra={"token": SECRET})
            try:
                raise RuntimeError(f"auth failed for Bearer {SECRET}")
            except RuntimeError:
                log.exception("request failed")
            log.debug("not exported")
        logging.getLogger("opentelemetry.exporter.x").warning("never exported")
    finally:
        log.removeHandler(handler)

    records = [r.log_record for r in exporter.get_finished_logs()]
    assert len(records) == 2
    blob = " ".join(str(r.body) + str(dict(r.attributes or {})) for r in records)
    assert SECRET not in blob
    assert "token" not in dict(records[0].attributes)  # `extra=` isn't exported
    assert records[0].trace_id == span.get_span_context().trace_id
    assert records[1].attributes["exception.type"] == "RuntimeError"


def test_redact_text():
    for leaked in (
        f"postgresql://app:{SECRET}@db:5432/x",
        f"Authorization: Bearer {SECRET}",
        f'{{"api_key": "{SECRET}"}}',
        f"/ui/?token={SECRET}",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        f"key {SECRET}",
    ):
        assert SECRET not in tx.redact_text(leaked)
        assert "eyJzdWIi" not in tx.redact_text(leaked)
    assert tx.redact_text("max_tokens=100 input_tokens=5") == "max_tokens=100 input_tokens=5"


def test_an_llm_call_stays_clean_even_when_other_calls_send_everything(memory, monkeypatch):
    # The provider's setting decides, whatever OTEL_HTTP_CLIENT_PROPAGATION says.
    telemetry._teardown()
    _reset_otel_globals()
    monkeypatch.setattr(telemetry, "_state", None)
    monkeypatch.setattr(settings, "otel_http_client_propagation", "all")
    assert telemetry.init_telemetry("backend")

    pool = _Pool()
    provider = _openai_provider(pool, propagate=False)

    async def call():
        with pytest.raises(openai.BadRequestError):
            await provider.complete([{"role": "user", "content": "hi"}], model="m", max_tokens=1)

    _inside_a_request(call)
    (headers,) = pool.sent
    assert "traceparent" not in headers and "baggage" not in headers
