"""Token exchange (external auth integration) tests."""

import uuid

_SUFFIX = uuid.uuid4().hex[:8]
_PROVIDER = f"partner-app-{_SUFFIX}"
_SUBJECT = f"ext_{_SUFFIX}"
_EMAIL = f"test_exchange_{_SUFFIX}@example.com"
_exchange_key = None
_exchange_key_id = None
_provisioned_user_id = None
_linked_user_id = None


def _exchange_headers():
    return {"X-API-Key": _exchange_key}


def teardown(ctx):
    for user_id in (_provisioned_user_id, _linked_user_id):
        if user_id:
            try:
                ctx.client.delete(f"/api/v1/users/{user_id}", headers=ctx.admin_headers())
            except Exception:
                pass
    if _exchange_key_id:
        try:
            ctx.client.delete(
                f"/api/v1/api-keys/{_exchange_key_id}", headers=ctx.admin_headers()
            )
        except Exception:
            pass


def test_01_create_exchange_api_key(ctx):
    global _exchange_key, _exchange_key_id
    r = ctx.client.post(
        "/api/v1/api-keys",
        headers=ctx.admin_headers(),
        json={
            "name": f"exchange-test-{_SUFFIX}",
            "permissions": {"sinas.auth.exchange:all": True},
        },
    )
    assert r.status_code in (200, 201), f"Create key failed: {r.text}"
    body = r.json()
    _exchange_key = body["key"]
    _exchange_key_id = body["id"]


def test_02_unknown_identity_404_without_provision(ctx):
    r = ctx.client.post(
        "/auth/token/exchange",
        headers=_exchange_headers(),
        json={"provider": _PROVIDER, "subject": _SUBJECT},
    )
    assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text}"


def test_03_auto_provision_requires_email(ctx):
    r = ctx.client.post(
        "/auth/token/exchange",
        headers=_exchange_headers(),
        json={"provider": _PROVIDER, "subject": _SUBJECT, "auto_provision": True},
    )
    assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"


def test_04_auto_provision_creates_user(ctx):
    global _provisioned_user_id
    r = ctx.client.post(
        "/auth/token/exchange",
        headers=_exchange_headers(),
        json={
            "provider": _PROVIDER,
            "subject": _SUBJECT,
            "email": _EMAIL,
            "auto_provision": True,
            "custom_fields": {"plan": "enterprise"},
            "metadata": {"source": "integration-test"},
        },
    )
    assert r.status_code == 200, f"Exchange failed: {r.text}"
    body = r.json()
    assert body["provisioned"] is True
    assert body["access_token"] and body["refresh_token"]
    assert body["user"]["email"] == _EMAIL
    _provisioned_user_id = body["user"]["id"]

    # The issued access token works as the provisioned user
    r = ctx.client.get(
        "/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["email"] == _EMAIL


def test_05_second_exchange_reuses_user(ctx):
    r = ctx.client.post(
        "/auth/token/exchange",
        headers=_exchange_headers(),
        json={"provider": _PROVIDER, "subject": _SUBJECT},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provisioned"] is False
    assert body["user"]["id"] == _provisioned_user_id


def test_06_custom_fields_merge_on_exchange(ctx):
    # Admin sets a field the partner doesn't know about
    r = ctx.client.patch(
        f"/api/v1/users/{_provisioned_user_id}",
        headers=ctx.admin_headers(),
        json={"custom_fields": {"plan": "enterprise", "admin_note": "keep"}},
    )
    assert r.status_code == 200, r.text

    r = ctx.client.post(
        "/auth/token/exchange",
        headers=_exchange_headers(),
        json={
            "provider": _PROVIDER,
            "subject": _SUBJECT,
            "custom_fields": {"plan": "pro"},
        },
    )
    assert r.status_code == 200, r.text

    r = ctx.client.get(
        f"/api/v1/users/{_provisioned_user_id}", headers=ctx.admin_headers()
    )
    fields = r.json()["custom_fields"]
    assert fields["plan"] == "pro", f"Partner key not updated: {fields}"
    assert fields["admin_note"] == "keep", f"Admin key clobbered: {fields}"


def test_07_email_fallback_links_identity(ctx):
    global _linked_user_id
    email2 = f"test_exchange2_{_SUFFIX}@example.com"
    r = ctx.client.post(
        "/api/v1/users", headers=ctx.admin_headers(), json={"email": email2}
    )
    assert r.status_code in (200, 201), r.text
    _linked_user_id = r.json()["id"]

    r = ctx.client.post(
        "/auth/token/exchange",
        headers=_exchange_headers(),
        json={"provider": _PROVIDER, "subject": f"other_{_SUFFIX}", "email": email2},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["id"] == _linked_user_id
    assert r.json()["provisioned"] is False

    # Identity is now linked: same exchange without email resolves the user
    r = ctx.client.post(
        "/auth/token/exchange",
        headers=_exchange_headers(),
        json={"provider": _PROVIDER, "subject": f"other_{_SUFFIX}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["id"] == _linked_user_id


def test_08_exchange_denied_without_permission(ctx):
    # Key with an unrelated permission must not be able to exchange
    r = ctx.client.post(
        "/api/v1/api-keys",
        headers=ctx.admin_headers(),
        json={
            "name": f"no-exchange-{_SUFFIX}",
            "permissions": {"sinas.users.read:own": True},
        },
    )
    assert r.status_code in (200, 201), r.text
    key = r.json()["key"]
    key_id = r.json()["id"]
    try:
        r = ctx.client.post(
            "/auth/token/exchange",
            headers={"X-API-Key": key},
            json={"provider": _PROVIDER, "subject": _SUBJECT},
        )
        assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"
    finally:
        ctx.client.delete(f"/api/v1/api-keys/{key_id}", headers=ctx.admin_headers())
