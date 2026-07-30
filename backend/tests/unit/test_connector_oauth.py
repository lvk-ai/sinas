"""Unit tests for connector OAuth token handling.

Covers the two regressions most likely to recur:
  - token expiry storage (a missing expires_in must not make a refreshable token
    look valid forever) — the #2 fix.
  - fail-closed auth: an unresolvable OAuth token must raise, not silently send an
    unauthenticated request — the #5 fix.

These avoid the DB/HTTP layers: _store_token_fields takes a plain row + payload, and
the OAuth auth branches short-circuit before touching the DB when no user/connector
context is present.
"""

import types

import pytest

from app.services.connector_service import (
    ConnectorAuthError,
    OAUTH_DEFAULT_TTL,
    connector_service,
)


@pytest.fixture(autouse=True)
def _identity_encryption(monkeypatch):
    """Make encrypt a no-op so _store_token_fields doesn't need a real Fernet key."""
    fake = types.SimpleNamespace(
        encrypt=lambda v: v, decrypt=lambda v: v
    )
    monkeypatch.setattr("app.services.connector_service.encryption_service", fake)


def _row(**kw):
    row = types.SimpleNamespace(
        encrypted_access_token="",
        encrypted_refresh_token="",
        scope=None,
        token_type=None,
        expires_at=None,
    )
    for k, v in kw.items():
        setattr(row, k, v)
    return row


def test_expires_in_present_sets_expiry():
    row = _row()
    connector_service._store_token_fields(
        row, {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
    )
    assert row.expires_at is not None


def test_missing_expires_in_with_refresh_token_gets_conservative_ttl():
    # The bug: without expires_in the token was treated as valid forever and never
    # refreshed. It must instead get a bounded expiry so the refresh path runs.
    row = _row()
    connector_service._store_token_fields(
        row, {"access_token": "a", "refresh_token": "r"}  # no expires_in
    )
    assert row.expires_at is not None
    assert connector_service._token_still_valid(row) is True  # valid now...
    # ...but bounded — an expiry roughly OAUTH_DEFAULT_TTL out, not None/forever.
    from datetime import datetime, timezone

    remaining = (row.expires_at - datetime.now(timezone.utc)).total_seconds()
    assert 0 < remaining <= OAUTH_DEFAULT_TTL + 5


def test_refresh_without_expires_in_does_not_clobber_known_expiry():
    # A refresh response omitting expires_in must not overwrite a known expiry with None
    # (which would permanently disable future refreshes).
    from datetime import datetime, timedelta, timezone

    known = datetime.now(timezone.utc) + timedelta(hours=2)
    row = _row(encrypted_refresh_token="r", expires_at=known)
    connector_service._store_token_fields(row, {"access_token": "a2"})  # refresh, no expires_in
    assert row.expires_at is not None


def test_no_expiry_no_refresh_token_left_untouched():
    row = _row(encrypted_refresh_token="")
    connector_service._store_token_fields(row, {"access_token": "a"})  # no expires_in, no refresh
    assert row.expires_at is None  # best-effort: usable, nothing to refresh with


@pytest.mark.asyncio
async def test_oauth_authcode_missing_context_raises():
    # No connector/user context → token unresolvable → must fail closed, not return {}.
    with pytest.raises(ConnectorAuthError):
        await connector_service._resolve_auth(
            db=None,
            auth_config={"type": "oauth2_authorization_code"},
            user_token=None,
            user_id=None,
            connector_id=None,
        )


@pytest.mark.asyncio
async def test_none_auth_still_returns_empty():
    # Regression guard: raising must be scoped to OAuth failures, not all auth.
    headers, query = await connector_service._resolve_auth(
        db=None, auth_config={"type": "none"}, user_token=None
    )
    assert headers == {} and query == {}
