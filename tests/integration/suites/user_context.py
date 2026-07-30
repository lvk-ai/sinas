"""User context injection tests (custom fields in query bind parameters).

The query tests need a reachable postgres to register as a database
connection. Set SINAS_TEST_QUERY_DB_URL (postgresql://user:pass@host:port/db)
to enable them; without it only the custom-fields setup test runs.
"""

import os
import uuid
from urllib.parse import urlparse

_SUFFIX = uuid.uuid4().hex[:8]
_NS = "test_user_ctx"
_QUERY_NAME = f"ctx_probe_{_SUFFIX}"
_DB_URL = os.environ.get("SINAS_TEST_QUERY_DB_URL")

_admin_user_id = None
_previous_custom_fields = None
_connection_id = None
_query_created = False


def teardown(ctx):
    if _query_created:
        try:
            ctx.client.delete(
                f"/api/v1/queries/{_NS}/{_QUERY_NAME}", headers=ctx.admin_headers()
            )
        except Exception:
            pass
    if _connection_id:
        try:
            ctx.client.delete(
                f"/api/v1/database-connections/{_connection_id}",
                headers=ctx.admin_headers(),
            )
        except Exception:
            pass
    if _admin_user_id is not None:
        try:
            ctx.client.patch(
                f"/api/v1/users/{_admin_user_id}",
                headers=ctx.admin_headers(),
                json={"custom_fields": _previous_custom_fields or {}},
            )
        except Exception:
            pass


def test_01_set_admin_custom_fields(ctx):
    global _admin_user_id, _previous_custom_fields
    r = ctx.client.get("/auth/me", headers=ctx.admin_headers())
    assert r.status_code == 200, r.text
    _admin_user_id = r.json()["id"]

    r = ctx.client.get(f"/api/v1/users/{_admin_user_id}", headers=ctx.admin_headers())
    assert r.status_code == 200, r.text
    _previous_custom_fields = r.json().get("custom_fields")

    r = ctx.client.patch(
        f"/api/v1/users/{_admin_user_id}",
        headers=ctx.admin_headers(),
        json={
            "custom_fields": {
                "region": "eu-west",
                "tier": 3,
                "nested": {"skipped": True},
                "bad-key!": "skipped",
            }
        },
    )
    assert r.status_code == 200, r.text


def test_02_create_database_connection(ctx):
    global _connection_id
    if not _DB_URL:
        return  # SINAS_TEST_QUERY_DB_URL not set
    parsed = urlparse(_DB_URL)
    r = ctx.client.post(
        "/api/v1/database-connections",
        headers=ctx.admin_headers(),
        json={
            "name": f"test-user-ctx-{_SUFFIX}",
            "connection_type": "postgresql",
            "host": parsed.hostname,
            "port": parsed.port or 5432,
            "database": parsed.path.lstrip("/"),
            "username": parsed.username,
            "password": parsed.password,
        },
    )
    assert r.status_code in (200, 201), f"Create connection failed: {r.text}"
    _connection_id = r.json()["id"]


def test_03_query_receives_user_custom_params(ctx):
    global _query_created
    if not _connection_id:
        return  # depends on test_02
    r = ctx.client.post(
        "/api/v1/queries",
        headers=ctx.admin_headers(),
        json={
            "namespace": _NS,
            "name": _QUERY_NAME,
            "description": "User context probe",
            "database_connection_id": _connection_id,
            "operation": "read",
            "sql": (
                "SELECT CAST(:user_custom_region AS TEXT) AS region, "
                "CAST(:user_custom_tier AS INT) AS tier, "
                "CAST(:user_email AS TEXT) AS email"
            ),
        },
    )
    assert r.status_code in (200, 201), f"Create query failed: {r.text}"
    _query_created = True

    r = ctx.client.post(
        f"/queries/{_NS}/{_QUERY_NAME}/execute",
        headers=ctx.admin_headers(),
        json={"input": {}},
    )
    assert r.status_code == 200, f"Execute failed: {r.text}"
    rows = r.json()["data"]
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["region"] == "eu-west", row
    assert row["tier"] == 3, row
    assert "@" in row["email"], row
