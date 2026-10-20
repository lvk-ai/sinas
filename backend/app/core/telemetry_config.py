"""Which OpenTelemetry exporters a process sends to, read from its environment.

Pure parsing: nothing here touches the OpenTelemetry SDK or the network, so it
is unit-tested on plain dicts. `app.core.telemetry` builds the pipeline from
what `resolve_exporters` returns.

Three sources, any combination of them active at once:

1. The standard OpenTelemetry variables (`OTEL_EXPORTER_OTLP_ENDPOINT`,
   `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `..._HEADERS`, `..._PROTOCOL`, ...).
   They make one exporter, `otlp`, whose endpoint, headers, timeout,
   compression and certificates the OpenTelemetry SDK reads itself, exactly
   as it documents them.
2. `OTEL_EXPORTERS`: a JSON list of named exporters, each with its own
   endpoint, headers, protocol and attribute preset, e.g. an LLM
   observability tool and an APM side by side. A secret header can stay out
   of the JSON: `OTEL_EXPORTER_<NAME>_HEADERS` (and `_ENDPOINT`) add to or
   override the entry named `<name>`, so it can come from a Kubernetes Secret.
3. The pre-0.5.0 single exporter (`OTEL_EXPORTER_ENDPOINT`,
   `OTEL_EXPORTER_HEADERS`, `OTEL_VENDOR`). Deprecated; still honoured.

Header values are credentials. They are never logged, never part of a repr,
and never in an error message: errors name the exporter and the header, not
the value.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

# Attribute presets: how the LLM attributes of Sinas's own spans are named on
# the way out (see telemetry_export.PRESETS). `none` sends no prompt/response
# content at all, for a general-purpose APM.
PRESET_NAMES = frozenset({"genai", "langfuse", "langwatch", "generic_otel", "none"})
DEFAULT_PRESET = "genai"

# Which spans an exporter receives. `all`: every span, auto-instrumented ones
# (HTTP server/client, SQL, Redis) included. `sinas`: only the spans Sinas
# records itself (conversation turns, LLM calls, tools, agent and function
# jobs): what an LLM-observability tool wants, and all that existed before
# auto-instrumentation.
SPAN_SCOPES = frozenset({"all", "sinas"})
# The LLM tools default to Sinas's own spans: SQL and Redis spans are noise
# there, and it is what they received before 0.5.0.
_SINAS_SCOPED_PRESETS = frozenset({"langfuse", "langwatch", "generic_otel"})

PROTOCOLS = frozenset({"http/protobuf", "grpc"})
SIGNALS = frozenset({"traces", "logs"})

# An exporter name becomes part of an environment variable name
# (OTEL_EXPORTER_<NAME>_HEADERS), so it is restricted to what such a name can
# hold. `otlp…` would collide with the standard variables
# (OTEL_EXPORTER_OTLP_HEADERS), `legacy` is the deprecated exporter's.
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_RESERVED = ("otlp", "legacy")


class ExporterConfigError(ValueError):
    """An exporter entry that can't be used. The message never holds a header value."""


@dataclass(frozen=True)
class ExporterConfig:
    name: str
    source: str  # "standard" | "list" | "legacy"
    protocol: str = "http/protobuf"
    logs_protocol: str = "http/protobuf"
    # None: the SDK resolves it from the standard variables (source "standard").
    # repr=False: a repr must never carry a credential (an endpoint may hold
    # one in its userinfo or query; `describe()` shows it safely).
    traces_endpoint: Optional[str] = field(default=None, repr=False)
    logs_endpoint: Optional[str] = field(default=None, repr=False)
    headers: Optional[dict[str, str]] = field(default=None, repr=False)
    preset: str = DEFAULT_PRESET
    spans: str = "all"
    signals: frozenset[str] = frozenset({"traces"})
    timeout: Optional[float] = None
    compression: Optional[str] = None
    insecure: Optional[bool] = None

    def describe(self) -> str:
        """For a log line: where it sends and how, without credentials."""
        where = safe_endpoint(self.traces_endpoint or self.logs_endpoint) if (
            self.traces_endpoint or self.logs_endpoint
        ) else "from OTEL_EXPORTER_OTLP_* variables"
        signals = "+".join(sorted(self.signals))
        return (
            f"{self.name} ({where}, {self.protocol}, preset={self.preset}, "
            f"spans={self.spans}, signals={signals})"
        )


def safe_endpoint(url: Optional[str]) -> str:
    """An endpoint as it may be logged: no userinfo, no query, no fragment."""
    if not url:
        return ""
    if "://" not in url:  # a gRPC host:port
        return url.split("?", 1)[0].rsplit("@", 1)[-1]
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid url>"
    host = parts.netloc.rsplit("@", 1)[-1]
    if not parts.scheme:
        return host or "<endpoint>"
    return f"{parts.scheme}://{host}{parts.path}"


def env_name(name: str) -> str:
    """`langfuse` -> `LANGFUSE`, `my-apm` -> `MY_APM` (for OTEL_EXPORTER_<NAME>_*)."""
    return re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def parse_headers(raw: Any, *, where: str) -> dict[str, str]:
    """Headers from a mapping, or from a `k=v,k2=v2` string.

    The string form is the OTel one: values may be URL-encoded
    (`Basic%20abc%3D`) and need not be (`sentry sentry_key=abc`); a value may
    hold `=`. Unlike the SDK's parser, a malformed pair is reported by
    position, never by content: header values are credentials.
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, Mapping):
        out: dict[str, str] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not key.strip():
                raise ExporterConfigError(f"{where}: header names must be non-empty strings")
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise ExporterConfigError(f"{where}: header {key!r} must have a string value")
            out[key.strip().lower()] = str(value)
        return out
    if not isinstance(raw, str):
        raise ExporterConfigError(f"{where}: headers must be an object or a 'k=v,k2=v2' string")
    out = {}
    for index, pair in enumerate(raw.split(","), start=1):
        if not pair.strip():
            continue
        name, sep, value = pair.partition("=")
        name = unquote(name).strip().lower()
        if not sep or not name or re.search(r"[\s\"(),/:;<=>?@\[\\\]{}]", name):
            raise ExporterConfigError(f"{where}: header #{index} is not in name=value form")
        out[name] = unquote(value).strip()
    return out


def parse_legacy_headers(raw: Optional[str]) -> dict[str, str]:
    """The pre-0.5.0 `OTEL_EXPORTER_HEADERS` format, parsed as it always was:
    comma-separated `k=v`, split on the first `=`, no URL-decoding."""
    headers: dict[str, str] = {}
    for pair in (raw or "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            headers[k.strip()] = v.strip()
    return headers


def _signal_urls(endpoint: str, protocol: str) -> tuple[str, str]:
    """(traces URL, logs URL) for an OTLP endpoint.

    For OTLP/HTTP an endpoint is the base the standard variable takes
    (`https://host/otlp` -> `.../v1/traces`, `.../v1/logs`) or a full
    signal URL as some vendors print it (`.../v1/traces`); for gRPC it is
    the server address, the same for every signal.
    """
    if protocol == "grpc":
        return endpoint, endpoint
    base = endpoint.rstrip("/")
    for suffix in ("/v1/traces", "/v1/logs"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}/v1/traces", f"{base}/v1/logs"


def _check_url(url: str, *, where: str, protocol: str) -> None:
    if protocol == "grpc" and "://" not in url and re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+:\d+", url):
        return  # host:port is a valid gRPC target
    try:
        parts = urlsplit(url)
    except ValueError as e:
        raise ExporterConfigError(f"{where}: endpoint is not a valid URL") from e
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ExporterConfigError(f"{where}: endpoint must be an absolute http(s) URL")


def _protocol(value: Any, *, where: str) -> str:
    protocol = str(value or "http/protobuf").strip().lower()
    if protocol in ("http", "http/proto"):
        protocol = "http/protobuf"
    if protocol not in PROTOCOLS:
        raise ExporterConfigError(
            f"{where}: protocol must be 'http/protobuf' or 'grpc' (http/json is not "
            "supported by the Python OpenTelemetry SDK)"
        )
    return protocol


def _preset(value: Any, *, where: str) -> str:
    preset = str(value or DEFAULT_PRESET).strip().lower()
    if preset not in PRESET_NAMES:
        raise ExporterConfigError(
            f"{where}: preset must be one of {', '.join(sorted(PRESET_NAMES))}"
        )
    return preset


def _spans(value: Any, preset: str, *, where: str) -> str:
    if value is None or value == "":
        return "sinas" if preset in _SINAS_SCOPED_PRESETS else "all"
    spans = str(value).strip().lower()
    if spans not in SPAN_SCOPES:
        raise ExporterConfigError(f"{where}: spans must be 'all' or 'sinas'")
    return spans


def _signals(value: Any, *, where: str) -> frozenset[str]:
    if value is None:
        return frozenset({"traces"})
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        raise ExporterConfigError(f"{where}: signals must be a list, e.g. [\"traces\", \"logs\"]")
    signals = frozenset(str(s).strip().lower() for s in items if str(s).strip())
    unknown = signals - SIGNALS
    if unknown or not signals:
        raise ExporterConfigError(f"{where}: signals must be 'traces' and/or 'logs'")
    return signals


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _env_list(value: Optional[str], default: str) -> list[str]:
    return [v.strip().lower() for v in (value if value is not None else default).split(",") if v.strip()]


def sdk_disabled(env: Mapping[str, str]) -> bool:
    return _truthy(env.get("OTEL_SDK_DISABLED"))


def standard_exporter(env: Mapping[str, str], *, preset: str) -> Optional[ExporterConfig]:
    """The exporter the standard OTEL_EXPORTER_OTLP_* variables describe, if any.

    Present when an OTLP endpoint is set (the SDK's own default,
    localhost:4318, is never assumed: nothing is sent unless asked). Its
    endpoints, headers, timeout, compression and certificates are left to
    the SDK, which reads the same variables.
    """
    traces_on = "otlp" in _env_list(env.get("OTEL_TRACES_EXPORTER"), "otlp")
    logs_on = "otlp" in _env_list(env.get("OTEL_LOGS_EXPORTER"), "otlp")
    has_traces = bool(env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))
    has_logs = bool(env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or env.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"))
    signals = set()
    if traces_on and has_traces:
        signals.add("traces")
    if logs_on and has_logs:
        signals.add("logs")
    if not signals:
        return None
    where = "OTEL_EXPORTER_OTLP_PROTOCOL"
    general = env.get("OTEL_EXPORTER_OTLP_PROTOCOL")
    return ExporterConfig(
        name="otlp",
        source="standard",
        protocol=_protocol(env.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") or general, where=where),
        logs_protocol=_protocol(env.get("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL") or general, where=where),
        preset=preset,
        spans=_spans(None, preset, where="otlp"),
        signals=frozenset(signals),
    )


def list_exporters(raw: Optional[str], env: Mapping[str, str]) -> tuple[list[ExporterConfig], list[str]]:
    """The exporters `OTEL_EXPORTERS` declares, with per-name env overrides.

    Returns (exporters, errors). A bad entry is reported and left out; the
    others still export (telemetry must never stop the platform).
    """
    if not raw or not raw.strip():
        return [], []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as e:
        # Position only: the JSON may hold header values.
        return [], [f"OTEL_EXPORTERS is not valid JSON (line {e.lineno}, column {e.colno})"]
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return [], ["OTEL_EXPORTERS must be a JSON list of exporter objects"]

    exporters: list[ExporterConfig] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        where = f"OTEL_EXPORTERS[{index}]"
        try:
            if not isinstance(entry, dict):
                raise ExporterConfigError(f"{where}: must be an object")
            name = str(entry.get("name") or "").strip().lower()
            if not _NAME.match(name):
                raise ExporterConfigError(
                    f"{where}: name must start with a letter and hold only a-z, 0-9, '_' or '-'"
                )
            if name.startswith(_RESERVED):
                raise ExporterConfigError(f"{where}: name {name!r} is reserved")
            if name in seen:
                raise ExporterConfigError(f"{where}: name {name!r} is used twice")
            where = f"OTEL_EXPORTERS[{name}]"
            if entry.get("enabled", True) is False:
                seen.add(name)
                continue
            prefix = f"OTEL_EXPORTER_{env_name(name)}_"
            protocol = _protocol(env.get(prefix + "PROTOCOL") or entry.get("protocol"), where=where)
            endpoint = env.get(prefix + "ENDPOINT") or entry.get("endpoint")
            traces_url = entry.get("traces_endpoint")
            logs_url = entry.get("logs_endpoint")
            if endpoint:
                if not isinstance(endpoint, str):
                    raise ExporterConfigError(f"{where}: endpoint must be a string")
                default_traces, default_logs = _signal_urls(endpoint.strip(), protocol)
                traces_url = traces_url or default_traces
                logs_url = logs_url or default_logs
            if not traces_url and not logs_url:
                raise ExporterConfigError(
                    f"{where}: needs an endpoint (or {prefix}ENDPOINT)"
                )
            for url in (traces_url, logs_url):
                if url:
                    _check_url(url, where=where, protocol=protocol)

            headers = parse_headers(entry.get("headers"), where=where)
            headers.update(parse_headers(env.get(prefix + "HEADERS"), where=f"{prefix}HEADERS"))

            preset = _preset(env.get(prefix + "PRESET") or entry.get("preset"), where=where)
            signals = _signals(entry.get("signals"), where=where)
            if "traces" in signals and not traces_url:
                raise ExporterConfigError(f"{where}: exports traces but has no traces endpoint")
            if "logs" in signals and not logs_url:
                raise ExporterConfigError(f"{where}: exports logs but has no logs endpoint")

            timeout = entry.get("timeout")
            if timeout is not None and (
                isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
            ):
                raise ExporterConfigError(f"{where}: timeout must be a positive number of seconds")
            compression = entry.get("compression")
            if compression is not None and str(compression).lower() not in ("gzip", "deflate", "none"):
                raise ExporterConfigError(f"{where}: compression must be gzip, deflate or none")
            insecure = entry.get("insecure")
            if insecure is not None and not isinstance(insecure, bool):
                raise ExporterConfigError(f"{where}: insecure must be true or false")

            exporters.append(
                ExporterConfig(
                    name=name,
                    source="list",
                    protocol=protocol,
                    logs_protocol=protocol,
                    traces_endpoint=traces_url,
                    logs_endpoint=logs_url,
                    headers=headers,
                    preset=preset,
                    spans=_spans(entry.get("spans"), preset, where=where),
                    signals=signals,
                    timeout=float(timeout) if timeout is not None else None,
                    compression=str(compression).lower() if compression is not None else None,
                    insecure=insecure,
                )
            )
            seen.add(name)
        except ExporterConfigError as e:
            errors.append(str(e))
    return exporters, errors


def legacy_exporter(
    endpoint: Optional[str], headers: Optional[str], vendor: Optional[str]
) -> tuple[Optional[ExporterConfig], list[str]]:
    """The pre-0.5.0 single exporter: a full OTLP/HTTP traces URL, `k=v`
    headers, and `OTEL_VENDOR` as its preset. Traces only, Sinas's own spans
    only: exactly what it received before."""
    if not endpoint:
        return None, []
    where = "OTEL_EXPORTER_ENDPOINT"
    try:
        _check_url(endpoint, where=where, protocol="http/protobuf")
        preset = _preset(vendor or "langwatch", where="OTEL_VENDOR")
    except ExporterConfigError as e:
        return None, [str(e)]
    _, logs_url = _signal_urls(endpoint, "http/protobuf")
    return (
        ExporterConfig(
            name="legacy",
            source="legacy",
            traces_endpoint=endpoint,
            logs_endpoint=logs_url,
            headers=parse_legacy_headers(headers),
            preset=preset,
            spans="sinas",
            signals=frozenset({"traces"}),
        ),
        [],
    )


def resolve_exporters(
    env: Mapping[str, str],
    *,
    exporters_json: Optional[str],
    standard_preset: str,
    legacy_endpoint: Optional[str] = None,
    legacy_headers: Optional[str] = None,
    legacy_vendor: Optional[str] = None,
) -> tuple[list[ExporterConfig], list[str]]:
    """Every exporter configured for this process, and the configuration
    errors found on the way (none of which holds a header value)."""
    if sdk_disabled(env):
        return [], []
    errors: list[str] = []
    exporters: list[ExporterConfig] = []
    try:
        standard = standard_exporter(env, preset=_preset(standard_preset, where="OTEL_PRESET"))
    except ExporterConfigError as e:
        standard, errors = None, [str(e)]
    if standard is not None:
        exporters.append(standard)
    listed, list_errors = list_exporters(exporters_json, env)
    exporters.extend(listed)
    errors.extend(list_errors)
    legacy, legacy_errors = legacy_exporter(legacy_endpoint, legacy_headers, legacy_vendor)
    errors.extend(legacy_errors)
    if legacy is not None:
        exporters.append(legacy)
    return exporters, errors


# ── HTTP server exclusions ──────────────────────────────────────────────────


def path_excluder(paths: str, regexes: Optional[str] = None):
    """A predicate on a request path: True for one not to trace.

    `paths` is Sinas's comma-separated list (`/health`, or `/ui/*` for a
    prefix); `regexes` the contrib instrumentations' convention
    (`OTEL_PYTHON_FASTAPI_EXCLUDED_URLS`, `OTEL_PYTHON_EXCLUDED_URLS`):
    comma-separated regular expressions searched in the path.
    """
    exact: set[str] = set()
    prefixes: list[str] = []
    for item in (paths or "").split(","):
        item = item.strip()
        if not item:
            continue
        if item.endswith("*"):
            prefixes.append(item[:-1])
        else:
            exact.add(item.rstrip("/") or "/")
    patterns = []
    for item in (regexes or "").split(","):
        if item.strip():
            try:
                patterns.append(re.compile(item.strip()))
            except re.error:
                pass
    prefix_tuple = tuple(prefixes)

    def excluded(path: str) -> bool:
        if (path.rstrip("/") or "/") in exact:
            return True
        if prefix_tuple and path.startswith(prefix_tuple):
            return True
        return any(p.search(path) for p in patterns)

    return excluded
