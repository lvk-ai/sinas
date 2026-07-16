"""User identities and custom fields tests."""

import uuid

_SUFFIX = uuid.uuid4().hex[:8]
_EMAIL = f"test_identity_{_SUFFIX}@example.com"
_PROVIDER = f"test-app-{_SUFFIX}"
_SUBJECT = f"usr_{_SUFFIX}"
_user_id = None


def teardown(ctx):
    if _user_id:
        try:
            ctx.client.delete(f"/api/v1/users/{_user_id}", headers=ctx.admin_headers())
        except Exception:
            pass


def test_01_create_user_with_identity_and_fields(ctx):
    global _user_id
    r = ctx.client.post(
        "/api/v1/users",
        headers=ctx.admin_headers(),
        json={
            "email": _EMAIL,
            "custom_fields": {"department": "qa", "locale": "nl-NL"},
            "identities": [{"provider": _PROVIDER, "subject": _SUBJECT}],
        },
    )
    assert r.status_code in (200, 201), f"Create failed: {r.text}"
    body = r.json()
    _user_id = body["id"]
    assert body["custom_fields"] == {"department": "qa", "locale": "nl-NL"}


def test_02_get_user_includes_identities(ctx):
    r = ctx.client.get(f"/api/v1/users/{_user_id}", headers=ctx.admin_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["custom_fields"]["department"] == "qa"
    identities = [(i["provider"], i["subject"]) for i in body["identities"]]
    assert (_PROVIDER, _SUBJECT) in identities


def test_03_lookup_by_identity(ctx):
    r = ctx.client.get(
        "/api/v1/users/by-identity",
        headers=ctx.admin_headers(),
        params={"provider": _PROVIDER, "subject": _SUBJECT},
    )
    assert r.status_code == 200, r.text
    assert r.json()["id"] == _user_id


def test_04_lookup_unknown_identity_404(ctx):
    r = ctx.client.get(
        "/api/v1/users/by-identity",
        headers=ctx.admin_headers(),
        params={"provider": _PROVIDER, "subject": "nonexistent"},
    )
    assert r.status_code == 404


def test_05_update_custom_fields(ctx):
    r = ctx.client.patch(
        f"/api/v1/users/{_user_id}",
        headers=ctx.admin_headers(),
        json={"custom_fields": {"department": "engineering"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["custom_fields"] == {"department": "engineering"}


def test_06_add_second_identity(ctx):
    r = ctx.client.post(
        f"/api/v1/users/{_user_id}/identities",
        headers=ctx.admin_headers(),
        json={
            "provider": f"{_PROVIDER}-2",
            "subject": _SUBJECT,
            "metadata": {"source": "integration-test"},
        },
    )
    assert r.status_code in (200, 201), r.text
    assert r.json()["metadata"] == {"source": "integration-test"}


def test_07_identity_conflict_409(ctx):
    # Create a second user, then try to claim the first user's identity
    email2 = f"test_identity2_{_SUFFIX}@example.com"
    r = ctx.client.post(
        "/api/v1/users", headers=ctx.admin_headers(), json={"email": email2}
    )
    assert r.status_code in (200, 201), r.text
    user2_id = r.json()["id"]
    try:
        r = ctx.client.post(
            f"/api/v1/users/{user2_id}/identities",
            headers=ctx.admin_headers(),
            json={"provider": _PROVIDER, "subject": _SUBJECT},
        )
        assert r.status_code == 409, f"Expected conflict, got {r.status_code}: {r.text}"
    finally:
        ctx.client.delete(f"/api/v1/users/{user2_id}", headers=ctx.admin_headers())


def test_08_remove_identity(ctx):
    r = ctx.client.delete(
        f"/api/v1/users/{_user_id}/identities",
        headers=ctx.admin_headers(),
        params={"provider": f"{_PROVIDER}-2", "subject": _SUBJECT},
    )
    assert r.status_code == 204, r.text
    r = ctx.client.get(f"/api/v1/users/{_user_id}", headers=ctx.admin_headers())
    providers = [i["provider"] for i in r.json()["identities"]]
    assert f"{_PROVIDER}-2" not in providers
    assert _PROVIDER in providers


def test_09_config_apply_user_with_identity(ctx):
    email3 = f"test_identity3_{_SUFFIX}@example.com"
    config = f"""
apiVersion: sinas.co/v1
kind: SinasConfig
metadata:
  name: test_identities_{_SUFFIX}
spec:
  users:
    - email: {email3}
      customFields:
        team: platform
      identities:
        - provider: {_PROVIDER}
          subject: cfg_{_SUFFIX}
"""
    r = ctx.client.post(
        "/api/v1/config/apply",
        headers=ctx.admin_headers(),
        json={"config": config, "dryRun": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("success"), f"Apply failed: {body}"

    r = ctx.client.get(
        "/api/v1/users/by-identity",
        headers=ctx.admin_headers(),
        params={"provider": _PROVIDER, "subject": f"cfg_{_SUFFIX}"},
    )
    assert r.status_code == 200, r.text
    user = r.json()
    assert user["email"] == email3
    assert user["custom_fields"] == {"team": "platform"}
    ctx.client.delete(f"/api/v1/users/{user['id']}", headers=ctx.admin_headers())
