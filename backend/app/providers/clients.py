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

# Per loop. More than a deployment has providers; past it, the least
# recently used client is dropped (not closed: a call may still be using it).
MAX_CLIENTS = 64

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
            clients.popitem(last=False)
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
        close = getattr(client, "close", None) or getattr(client, "aclose", None)
        try:
            result = close() if close else None
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.debug(f"Closing an LLM client failed: {e}")
