"""SDK clients shared across provider instances.

A provider is built for every LLM call (each message, each tool round), and
each used to build its own SDK client. A new client has no open
connections, so every call paid a new TCP and TLS handshake to the model
API: about 60 to 130 ms measured, before the first token. Clients are now
kept per event loop and per connection settings, so calls reuse warm
connections. Provider instances are still built per call, so per-agent
settings (overrides, trace context) stay per call.

Per event loop because an async HTTP client's connections belong to the
loop that opened them.
"""
import asyncio
import hashlib
import logging
import weakref
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")
H = TypeVar("H")

# Per loop. More than a deployment has providers; past it, the least
# recently used client is dropped, and closed once any call still using it
# has had time to finish (longer than the SDKs' 10-minute request timeout).
MAX_CLIENTS = 64
CLOSE_EVICTED_AFTER_S = 15 * 60

_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, OrderedDict[Hashable, Any]]" = (
    weakref.WeakKeyDictionary()
)


def secret_id(secret: Optional[str]) -> str:
    """Stands for a secret in a cache key, so the key doesn't hold it."""
    return hashlib.sha256(secret.encode()).hexdigest() if secret else ""


def shared_client(key: Hashable, make: Callable[[], T]) -> T:
    """The client for these settings on the running loop, made on first use.

    Outside a running loop (a sync caller), a new client every time.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return make()
    clients = _clients.get(loop)
    if clients is None:
        clients = _clients[loop] = OrderedDict()
    client = clients.get(key)
    if client is None or _is_closed(client):
        client = clients[key] = make()
        while len(clients) > MAX_CLIENTS:
            _, evicted = clients.popitem(last=False)
            loop.call_later(CLOSE_EVICTED_AFTER_S, lambda c=evicted: loop.create_task(_close(c)))
    else:
        clients.move_to_end(key)
    return client


def _is_closed(client: Any) -> bool:
    is_closed = getattr(client, "is_closed", None)
    try:
        return bool(is_closed()) if callable(is_closed) else bool(is_closed)
    except Exception:
        return False


async def close_shared_clients() -> None:
    """Close the running loop's clients (on shutdown)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    clients = _clients.pop(loop, None) or {}
    for client in clients.values():
        await _close(client)


async def _close(client: Any) -> None:
    close = getattr(client, "close", None) or getattr(client, "aclose", None)
    try:
        result = close() if close else None
        if asyncio.iscoroutine(result):
            await result
    except Exception as e:
        logger.debug(f"Closing an LLM client failed: {e}")


# ── Trace context ───────────────────────────────────────────────────────────
#
# Whether a request to a model API carries the trace context (`traceparent`,
# `baggage`, ...) is Sinas's decision alone: the provider's `propagate_context`
# setting, off by default, because the baggage says whom a request is for.
# An SDK may decide otherwise on its own: anthropic 1.14.0 started sending its
# own call span's context, with the current baggage, on every request. So
# every SDK client's HTTP client gets a last request hook that drops the trace
# context from any request a provider did not mark as carrying it on purpose.
# The mark is a private header that the hook takes off again, so it never
# leaves the process either.

#: Set (by `BaseLLMProvider._sdk_context_headers`) on a request that carries
#: the trace context on purpose. Never sent.
CONTEXT_OPT_IN_HEADER = "x-sinas-propagate-context"

_W3C_CONTEXT_FIELDS = ("traceparent", "tracestate", "baggage")


def trace_context_fields() -> set[str]:
    """The headers that carry a trace context: the W3C ones, and whatever the
    registered propagator sets (`OTEL_PROPAGATORS` may add others, e.g. B3)."""
    fields = set(_W3C_CONTEXT_FIELDS)
    try:
        from opentelemetry.propagate import get_global_textmap

        fields.update(field.lower() for field in get_global_textmap().fields)
    except Exception:
        pass
    return fields


async def _drop_unrequested_trace_context(request: Any) -> None:
    if CONTEXT_OPT_IN_HEADER in request.headers:
        del request.headers[CONTEXT_OPT_IN_HEADER]
        return
    for field in trace_context_fields():
        if field in request.headers:
            del request.headers[field]


def guard_trace_context(http_client: H) -> H:
    """`http_client` (an `httpx` or `httpx2` async client), made to send trace
    context only on requests a provider marked with `CONTEXT_OPT_IN_HEADER`.

    The hook goes last, after any the client already has, so that it sees the
    request as it will be sent. Guarding a client twice adds it once.
    """
    hooks = http_client.event_hooks  # type: ignore[attr-defined]
    request_hooks = list(hooks.get("request", []))
    if _drop_unrequested_trace_context not in request_hooks:
        http_client.event_hooks = {  # type: ignore[attr-defined]
            **hooks,
            "request": [*request_hooks, _drop_unrequested_trace_context],
        }
    return http_client


# ── Budget refusals are final ──────────────────────────────────────────────
#
# The SDKs retry a 429 (and 408, 409, 5xx) on their own, twice by default,
# with backoff. A gateway that refuses a call because a budget is spent may
# answer 429; retrying that only spends time (and, at some gateways, counts
# as more refused calls). So a response the SDK would retry is classified
# first (`errors.classify`, with the provider's own `budget_errors` rules),
# and a budget refusal is marked `x-should-retry: false`, which both SDKs
# obey. The provider's rules travel to the hook as a private request header,
# taken off again before the request is sent.

#: Carries a provider's `budget_errors` rules (JSON) to the response hook.
#: Never sent.
BUDGET_RULES_HEADER = "x-sinas-budget-rules"
_RULES_EXTENSION = "sinas_budget_rules"


def _sdk_would_retry(status: int) -> bool:
    return status in (408, 409, 429) or status >= 500


async def _take_budget_rules(request: Any) -> None:
    value = request.headers.get(BUDGET_RULES_HEADER)
    if value is not None:
        del request.headers[BUDGET_RULES_HEADER]
        request.extensions = {**request.extensions, _RULES_EXTENSION: value}


async def _final_budget_refusal(response: Any) -> None:
    if not _sdk_would_retry(response.status_code):
        return
    from .errors import BUDGET_EXCEEDED, classify, parse_rules_header

    try:
        await response.aread()
        try:
            body: Any = response.json()
        except Exception:
            body = response.text
        rules = parse_rules_header(response.request.extensions.get(_RULES_EXTENSION))
        if classify(response.status_code, body, "", rules) == BUDGET_EXCEEDED:
            response.headers["x-should-retry"] = "false"
    except Exception as e:  # the SDK's own handling goes on regardless
        logger.debug(f"Classifying an LLM error response failed: {e}")


def guard_client(http_client: H) -> H:
    """`http_client` guarded for an LLM SDK: trace context only on request
    (`guard_trace_context`), and budget refusals never retried by the SDK.
    Guarding a client twice adds each hook once."""
    guard_trace_context(http_client)
    hooks = http_client.event_hooks  # type: ignore[attr-defined]
    request_hooks = list(hooks.get("request", []))
    response_hooks = list(hooks.get("response", []))
    if _take_budget_rules not in request_hooks:
        request_hooks.append(_take_budget_rules)
    if _final_budget_refusal not in response_hooks:
        response_hooks.append(_final_budget_refusal)
    http_client.event_hooks = {  # type: ignore[attr-defined]
        **hooks, "request": request_hooks, "response": response_hooks,
    }
    return http_client
