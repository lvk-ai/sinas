"""Short-lived, single-use state for the connector OAuth authorization-code flow.

The provider does not send our bearer token on its redirect, so we cannot authenticate
the callback directly. Instead the authenticated "begin" endpoint mints an opaque state,
stores the initiating user + connector (and the PKCE verifier) in Redis under it, and the
public callback looks the request back up by state. State is deleted on first use (CSRF /
replay protection) and expires on its own after STATE_TTL_SECONDS.

The state also carries a `browser_nonce` that the begin endpoint sets as an HttpOnly
cookie in the initiating browser; the callback requires the cookie to match. This binds
the flow to the browser that started it, preventing OAuth account-linking / login-CSRF:
an attacker who mints a state and hands its authorize URL to a victim cannot capture the
victim's tokens, because the victim's browser won't carry the attacker's nonce cookie.
"""
import base64
import hashlib
import json
import secrets
from typing import Optional

from app.core.redis import get_redis

STATE_TTL_SECONDS = 600  # 10 minutes to complete the consent screen
BIND_COOKIE_NAME = "sinas_coauth"  # HttpOnly cookie carrying the browser-binding nonce
_KEY_PREFIX = "connector_oauth_state:"


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


async def store_state(
    *, user_id: str, namespace: str, name: str, code_verifier: str, browser_nonce: str
) -> str:
    """Persist flow context under a fresh opaque state token and return the token."""
    state = secrets.token_urlsafe(32)
    payload = json.dumps(
        {
            "user_id": user_id,
            "namespace": namespace,
            "name": name,
            "code_verifier": code_verifier,
            "browser_nonce": browser_nonce,
        }
    )
    redis = await get_redis()
    await redis.set(_KEY_PREFIX + state, payload, ex=STATE_TTL_SECONDS)
    return state


def generate_browser_nonce() -> str:
    """Return an opaque nonce to bind an authorization flow to one browser."""
    return secrets.token_urlsafe(32)


async def consume_state(state: str) -> Optional[dict]:
    """Fetch and delete the flow context for a state. Returns None if unknown/expired."""
    if not state:
        return None
    redis = await get_redis()
    key = _KEY_PREFIX + state
    # GETDEL makes state single-use even under concurrent callbacks.
    raw = await redis.getdel(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None
