"""Callback URL validation + delivery for `/functions/.../execute/async`.

Three-state policy via the `CALLBACK_URL_HOSTS` env var (see `core/config.py`):
- unset / empty: callbacks disabled — any caller-supplied URL → ValueError.
- "*": permissive — accept any HTTPS URL that resolves to a non-private address.
- comma-separated host list: exact-host allowlist.

All checks happen *before* enqueue. SSRF guards (HTTPS scheme + non-private
resolved IP) always apply, regardless of allowlist mode.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

from app.core.config import settings

logger = logging.getLogger(__name__)


class CallbackURLError(ValueError):
    """Raised when a callback URL is rejected by policy."""


def _allowlist() -> set[str] | None:
    """
    Returns:
      - None when the feature is disabled (unset / empty config).
      - {"*"} when permissive mode is on.
      - Otherwise a set of exact hosts.
    """
    raw = (settings.callback_url_hosts or "").strip()
    if not raw:
        return None
    if raw == "*":
        return {"*"}
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _check_not_private(host: str) -> None:
    try:
        resolved_ip = socket.getaddrinfo(host, None, socket.AF_UNSPEC)[0][4][0]
    except socket.gaierror as e:
        raise CallbackURLError(f"Could not resolve callback host '{host}'") from e
    try:
        ip = ipaddress.ip_address(resolved_ip)
    except ValueError as e:
        raise CallbackURLError(f"Resolved address for '{host}' is not a valid IP") from e
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        raise CallbackURLError(
            f"Callback URLs must not resolve to private / loopback / link-local addresses (got {resolved_ip})"
        )


def validate_callback_url(url: str | None) -> str | None:
    """Validate a caller-supplied callback URL against the active policy.

    Returns the normalized URL on success, or None if no URL was supplied.
    Raises CallbackURLError on any rejection.
    """
    if not url:
        return None

    allowlist = _allowlist()
    if allowlist is None:
        raise CallbackURLError(
            "Callback URLs are not enabled on this instance "
            "(CALLBACK_URL_HOSTS is unset). Contact your administrator."
        )

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise CallbackURLError("callback_url must use https://")
    host = (parsed.hostname or "").lower()
    if not host:
        raise CallbackURLError("callback_url has no host")

    if "*" not in allowlist and host not in allowlist:
        raise CallbackURLError(
            f"Callback host '{host}' is not in CALLBACK_URL_HOSTS allowlist"
        )

    _check_not_private(host)

    # Strip trailing fragments — meaningless for server-to-server POSTs.
    if parsed.fragment:
        url = url.split("#", 1)[0]
    return url
