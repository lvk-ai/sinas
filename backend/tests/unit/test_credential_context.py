"""Session-scoped attribution context on tokens and API keys.

Pinned here: token exchange (only its trusted caller) sets a context, which
the access and refresh tokens carry and every refresh keeps; with
ATTRIBUTION_MEMBERSHIPS set, a context outside the user's memberships is
refused at exchange, at sign-in and at refresh; an API key created with a
session copies its context, can't be given another by its user, and only an
admin holding sinas.api_keys.assign_context:all sets one; requests carry the
token's or key's context, and a new chat's attribution snapshot takes it
over the user's fields; tokens from before (without a context) still work;
an SSO session claim mapping puts its claim in the login's context.
"""
import uuid

import pytest
from jose import jwt as jose_jwt
from sqlalchemy import select

from app.core.auth import create_access_token, current_credential_context
from app.core.config import settings
from app.models.agent import Agent
from app.models.chat import Chat
from app.models.user import APIKey, RefreshToken, Role, RolePermission, User, UserRole
from app.providers.attribution import (
    membership_problem,
    memberships,
    snapshot_from_fields,
    validate_context,
)
from tests.conftest import auth_headers


def _uid() -> str:
    return uuid.uuid4().hex[:8]


async def _user_with(db, *permissions: str, custom_fields=None) -> User:
    role = Role(name=f"ctx-{_uid()}", description="test")
    db.add(role)
    await db.flush()
    for key in permissions:
        db.add(RolePermission(role_id=role.id, permission_key=key, permission_value=True))
    user = User(email=f"ctx-{_uid()}@example.com", custom_fields=custom_fields)
    db.add(user)
    await db.flush()
    db.add(UserRole(role_id=role.id, user_id=user.id, active=True))
    await db.flush()
    return user


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _claims(token: str) -> dict:
    return jose_jwt.get_unverified_claims(token)


@pytest.fixture
def memberships_on(monkeypatch):
    monkeypatch.setattr(settings, "attribution_memberships", "orgId:orgs")
    monkeypatch.setattr(settings, "attribution_fields", "orgId")


# ── pure parts ──────────────────────────────────────────────────────────────


def test_contexts_are_validated():
    assert validate_context({"orgId": " org-2 ", "seats": 3}) == {"orgId": "org-2", "seats": 3}
    assert validate_context(None) == {}
    for bad in ({"org-id": "x"}, {"orgId": ""}, {"orgId": "a\r\nb"}, {"orgId": ["a"]}, {"orgId": {"a": 1}},
                {"orgId": "x" * 256}, {f"k{i}": "v" for i in range(21)}, "orgId=x"):
        with pytest.raises(ValueError):
            validate_context(bad)


def test_membership_checks(memberships_on):
    fields = {"orgs": ["org-1", "org-2"]}
    assert memberships(fields) == {"orgId": ["org-1", "org-2"]}
    assert membership_problem({"orgId": "org-2"}, fields) is None
    assert "member" in membership_problem({"orgId": "org-3"}, fields)
    # No listing: no membership at all.
    assert membership_problem({"orgId": "org-1"}, {}) is not None
    # Fields not membership-checked pass.
    assert membership_problem({"team": "x"}, {}) is None


def test_membership_checks_are_off_unless_configured(monkeypatch):
    monkeypatch.setattr(settings, "attribution_memberships", None)
    assert membership_problem({"orgId": "anything"}, {}) is None


def test_the_context_wins_over_the_users_fields_key_by_key(monkeypatch):
    monkeypatch.setattr(settings, "attribution_fields", "orgId,costCenter")
    fields = {"orgId": "org-1", "costCenter": "cc-9"}
    assert snapshot_from_fields(fields, {"orgId": "org-2"}) == {"orgId": "org-2", "costCenter": "cc-9"}
    assert snapshot_from_fields(fields, None) == fields


# ── token exchange and refresh ──────────────────────────────────────────────


async def _exchange(client, exchanger, **body):
    payload = {"provider": "partner", "subject": f"s-{_uid()}", "email": f"x-{_uid()}@example.com",
               "auto_provision": True, **body}
    return await client.post("/auth/token/exchange", json=payload, headers=auth_headers(exchanger))


async def test_exchange_sets_a_context_the_tokens_and_refresh_keep(client, db, memberships_on):
    exchanger = await _user_with(db, "sinas.auth.exchange:all")
    r = await _exchange(client, exchanger, custom_fields={"orgs": ["org-1", "org-2"]}, context={"orgId": "org-2"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert _claims(body["access_token"])["context"] == {"orgId": "org-2"}

    r = await client.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 200, r.text
    assert _claims(r.json()["access_token"])["context"] == {"orgId": "org-2"}
    # Nothing in a refresh request can change it.
    r = await client.post("/auth/refresh", json={"refresh_token": body["refresh_token"], "context": {"orgId": "org-1"}})
    assert r.status_code in (200, 422)
    if r.status_code == 200:
        assert _claims(r.json()["access_token"])["context"] == {"orgId": "org-2"}


async def test_exchange_refuses_a_context_outside_the_users_memberships(client, db, memberships_on):
    exchanger = await _user_with(db, "sinas.auth.exchange:all")
    r = await _exchange(client, exchanger, custom_fields={"orgs": ["org-1"]}, context={"orgId": "org-2"})
    assert r.status_code == 403
    assert "member" in r.json()["detail"]
    r = await _exchange(client, exchanger, context={"orgId": "org-1"})  # no memberships at all
    assert r.status_code == 403
    r = await _exchange(client, exchanger, context={"org-id": "x"})
    assert r.status_code == 422


async def test_only_the_trusted_exchanger_can_set_a_context(client, db):
    someone = await _user_with(db, "sinas.api_keys.create:own")
    r = await _exchange(client, someone, context={"orgId": "org-1"})
    assert r.status_code == 403


async def test_a_refresh_ends_when_the_membership_does(client, db, memberships_on):
    exchanger = await _user_with(db, "sinas.auth.exchange:all")
    r = await _exchange(client, exchanger, custom_fields={"orgs": ["org-1"]}, context={"orgId": "org-1"})
    body = r.json()
    user = await db.get(User, uuid.UUID(body["user"]["id"]))
    user.custom_fields = {"orgs": ["org-9"]}
    await db.flush()
    r = await client.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 401


async def test_a_session_without_context_stays_without(client, db):
    exchanger = await _user_with(db, "sinas.auth.exchange:all")
    body = (await _exchange(client, exchanger)).json()
    assert "context" not in _claims(body["access_token"])
    row = (await db.execute(select(RefreshToken).order_by(RefreshToken.created_at.desc()).limit(1))).scalar_one()
    assert row.context is None


# ── requests carry it; chats take it ────────────────────────────────────────


async def _agent(db) -> Agent:
    agent = Agent(namespace="ctx", name=f"a-{_uid()}", system_prompt="t", is_active=True)
    db.add(agent)
    await db.flush()
    return agent


async def test_a_new_chat_takes_the_tokens_context_over_the_users_fields(client, db, monkeypatch):
    monkeypatch.setattr(settings, "attribution_fields", "orgId")
    user = await _user_with(db, "sinas.agents/*/*.chat:all", custom_fields={"orgId": "org-1"})
    agent = await _agent(db)
    token = create_access_token(str(user.id), user.email, context={"orgId": "org-2"})
    r = await client.post(f"/agents/{agent.namespace}/{agent.name}/chats", json={}, headers=_bearer(token))
    assert r.status_code == 200, r.text
    chat = await db.get(Chat, uuid.UUID(r.json()["id"]))
    assert chat.attribution == {"orgId": "org-2"}

    # An old token (no context claim) still works, attributed by the fields.
    r = await client.post(f"/agents/{agent.namespace}/{agent.name}/chats", json={}, headers=auth_headers(user))
    assert r.status_code == 200, r.text
    chat = await db.get(Chat, uuid.UUID(r.json()["id"]))
    assert chat.attribution == {"orgId": "org-1"}


async def test_a_forged_or_invalid_context_claim_is_none(db):
    """A context claim only exists in a token Sinas signed; one that fails
    validation (written before a rule existed) binds nothing."""
    from app.core.auth import bind_credential_context, reset_credential_context

    token = bind_credential_context({"orgId": "a\nb"})
    try:
        assert current_credential_context() is None
    finally:
        reset_credential_context(token)


# ── API keys ────────────────────────────────────────────────────────────────


async def test_a_key_copies_the_creating_sessions_context(client, db):
    user = await _user_with(db, "sinas.api_keys.create:own", "sinas.api_keys.read:own")
    token = create_access_token(str(user.id), user.email, context={"orgId": "org-2"})
    r = await client.post(
        "/api/v1/api-keys", json={"name": "run-1", "permissions": {"sinas.api_keys.create:own": True}},
        headers=_bearer(token),
    )
    assert r.status_code == 201, r.text
    assert r.json()["context"] == {"orgId": "org-2"}
    key = await db.get(APIKey, uuid.UUID(r.json()["id"]))
    assert key.context == {"orgId": "org-2"}

    # Requests made with the key carry it.
    from app.core.auth import validate_api_key

    assert await validate_api_key(db, r.json()["key"]) is not None
    assert current_credential_context() == {"orgId": "org-2"}

    # A key created with that key carries the same context, never another.
    r2 = await client.post("/api/v1/api-keys", json={"name": "child"}, headers=_bearer(r.json()["key"]))
    assert r2.status_code == 201, r2.text
    assert r2.json()["context"] == {"orgId": "org-2"}


async def test_a_user_cant_give_a_key_another_context(client, db):
    user = await _user_with(db, "sinas.api_keys.create:own", "sinas.api_keys.update:own")
    token = create_access_token(str(user.id), user.email, context={"orgId": "org-2"})
    r = await client.post("/api/v1/api-keys", json={"name": "k", "context": {"orgId": "org-1"}}, headers=_bearer(token))
    assert r.status_code == 403
    # Nor change it afterwards: an update with a context is refused (and
    # one without leaves it as it is).
    r = await client.post("/api/v1/api-keys", json={"name": "k"}, headers=_bearer(token))
    key_id = r.json()["id"]
    for body in ({"context": {"orgId": "org-1"}}, {"context": None}):
        r = await client.patch(f"/api/v1/api-keys/{key_id}", json=body, headers=_bearer(token))
        assert r.status_code == 403, r.text
    r = await client.patch(f"/api/v1/api-keys/{key_id}", json={"name": "renamed"}, headers=_bearer(token))
    assert r.status_code == 200, r.text
    assert r.json()["context"] == {"orgId": "org-2"}
    key = await db.get(APIKey, uuid.UUID(key_id))
    await db.refresh(key)
    assert key.context == {"orgId": "org-2"}


async def test_an_admin_with_the_permission_sets_a_keys_context(client, db):
    admin = await _user_with(db, "sinas.api_keys.create:own", "sinas.api_keys.assign_context:all")
    r = await client.post(
        "/api/v1/api-keys", json={"name": "svc", "context": {"orgId": "org-7"}}, headers=auth_headers(admin)
    )
    assert r.status_code == 201, r.text
    assert r.json()["context"] == {"orgId": "org-7"}
    r = await client.post(
        "/api/v1/api-keys", json={"name": "bad", "context": {"orgId": "a\nb"}}, headers=auth_headers(admin)
    )
    assert r.status_code == 422


async def test_an_admin_with_the_permission_changes_a_keys_context(client, db):
    admin = await _user_with(
        db, "sinas.api_keys.create:own", "sinas.api_keys.update:own", "sinas.api_keys.assign_context:all"
    )
    r = await client.post("/api/v1/api-keys", json={"name": "svc", "context": {"orgId": "org-7"}}, headers=auth_headers(admin))
    key_id = r.json()["id"]
    r = await client.patch(f"/api/v1/api-keys/{key_id}", json={"context": {"orgId": "org-8"}}, headers=auth_headers(admin))
    assert r.status_code == 200, r.text
    assert r.json()["context"] == {"orgId": "org-8"}
    r = await client.patch(f"/api/v1/api-keys/{key_id}", json={"context": {"org-id": "x"}}, headers=auth_headers(admin))
    assert r.status_code == 422
    r = await client.patch(f"/api/v1/api-keys/{key_id}", json={"context": None}, headers=auth_headers(admin))
    assert r.status_code == 200 and r.json()["context"] is None


async def test_keys_created_without_a_session_context_have_none(client, db):
    from app.core.auth import create_api_key

    user = await _user_with(db, "sinas.api_keys.create:own")
    key, _ = await create_api_key(db, user, "deploy", {})
    assert key.context is None
    r = await client.post("/api/v1/api-keys", json={"name": "plain"}, headers=auth_headers(user))
    assert r.json()["context"] is None


def test_assign_context_is_in_no_default_role():
    from app.core.permissions import DEFAULT_ROLE_PERMISSIONS

    for role, perms in DEFAULT_ROLE_PERMISSIONS.items():
        assert "sinas.api_keys.assign_context:all" not in perms, role


# ── SSO session claims ──────────────────────────────────────────────────────


def test_session_claim_mappings_make_the_logins_context():
    from app.models.identity_provider import IdentityProvider
    from app.services import sso

    provider = IdentityProvider(name="idp", claim_mappings=[
        {"claim": "org_id", "field": "orgId", "target": "session"},
        {"claim": "plan", "field": "plan"},
    ])
    claims = {"org_id": "org-2", "plan": "pro"}
    assert sso.session_context(provider, claims) == {"orgId": "org-2"}
    user = User(email="a@example.com", custom_fields={})
    sso.sync_custom_fields(user, provider, claims)
    assert user.custom_fields == {"plan": "pro"}  # the session claim isn't stored on the user


def test_an_unusable_session_claim_refuses_the_login_rather_than_dropping_the_context():
    from app.models.identity_provider import IdentityProvider
    from app.services import sso

    provider = IdentityProvider(name="idp", display_name="IdP", claim_mappings=[
        {"claim": "org_id", "field": "orgId", "target": "session"},
        {"claim": "cost_center", "field": "costCenter", "target": "session"},
    ])
    for claims in (
        {"org_id": "org-2", "cost_center": "café"},  # not header-safe
        {"org_id": ["org-1", "org-2"]},  # several values for one session
        {"org_id": {"id": "org-2"}},  # an object
        {"org_id": "x" * 2000},  # overlong
    ):
        with pytest.raises(sso.SsoError):
            sso.session_context(provider, claims)
    # Absent (or empty): no context field, the login goes on.
    assert sso.session_context(provider, {"org_id": "org-2"}) == {"orgId": "org-2"}
    assert sso.session_context(provider, {"org_id": "", "cost_center": []}) == {}


async def test_a_component_keeps_the_context_it_was_rendered_under(client, db, monkeypatch):
    """A component page rendered in an org-2 session (or for an org-2 key)
    makes its requests, and the chats it starts, in org-2."""
    from fastapi import Request

    from app.core.auth import _verify_component_token, bind_credential_context, reset_credential_context
    from app.models.component import Component
    from app.services.component_access import component_token_claims, generate_component_access_token
    from app.services.content_tokens import generate_component_render_token

    user = await _user_with(db, "sinas.api_keys.create:own")
    component = Component(namespace="ctx", name=f"c-{_uid()}", user_id=user.id, is_active=True, source_code="x")
    db.add(component)
    await db.flush()

    token = bind_credential_context({"orgId": "org-2"})
    try:
        render = generate_component_render_token(component.namespace, component.name, str(user.id))
    finally:
        reset_credential_context(token)
    assert _claims(render)["context"] == {"orgId": "org-2"}

    access = generate_component_access_token(
        str(user.id), component.namespace, component.name, context=_claims(render)["context"]
    )
    request = Request({"type": "http", "path_params": {}, "headers": [],
                       "route": type("R", (), {"path": "/components/{ns}/{name}/access-token"})()})
    monkeypatch.setattr("app.services.component_access.route_allowed", lambda *a, **k: True)
    bind_credential_context(None)
    await _verify_component_token(component_token_claims(access), request, db)
    assert current_credential_context() == {"orgId": "org-2"}
    assert request.state.component_scope.context == {"orgId": "org-2"}

    # Rendered for an API key: the key's own stored context.
    key = APIKey(user_id=user.id, name="k", key_hash=_uid() * 8, key_prefix="sk-x", permissions={},
                 is_active=True, context={"orgId": "org-7"})
    db.add(key)
    await db.flush()
    access = generate_component_access_token(
        str(user.id), component.namespace, component.name, api_key_id=str(key.id), context={"orgId": "forged"}
    )
    await _verify_component_token(component_token_claims(access), request, db)
    assert current_credential_context() == {"orgId": "org-7"}


async def test_an_sso_login_carries_its_session_context(client, db, idp, memberships_on):
    from tests.unit.test_oidc_sso import _claims as oidc_claims, _login, _provider

    provider = await _provider(db, claim_mappings=[
        {"claim": "org_id", "field": "orgId", "target": "session"},
        {"claim": "orgs", "field": "orgs"},
    ])
    claims = oidc_claims(f"sso-{_uid()}@acme.com", org_id="org-2", orgs=["org-1", "org-2"])
    body = await _login(client, provider, idp, claims)
    assert _claims(body["access_token"])["context"] == {"orgId": "org-2"}


async def test_an_sso_login_outside_the_memberships_is_refused(client, db, idp, memberships_on):
    from tests.unit.test_oidc_sso import _callback, _claims as oidc_claims, _provider

    provider = await _provider(db, claim_mappings=[
        {"claim": "org_id", "field": "orgId", "target": "session"},
        {"claim": "orgs", "field": "orgs"},
    ])
    claims = oidc_claims(f"sso-{_uid()}@acme.com", org_id="org-3", orgs=["org-1"])
    params = await _callback(client, provider, idp, claims)
    assert "code" not in params and "member" in str(params)


from tests.unit.test_oidc_sso import idp  # noqa: E402,F401 - the fake IdP fixture


async def test_a_queued_job_carries_the_enqueuing_requests_context(db, monkeypatch):
    """A queued turn (and a resume, a delegate resume) records the request's
    context at enqueue and binds it in the job; a job delivered from a stored
    key id alone (a wakeup, a watch) binds the key's stored context."""
    from app.core.auth import bind_credential_context, reset_credential_context
    from app.queue import agent_jobs
    from app.services import queue_service as qs

    captured = []

    class _Pool:
        async def enqueue_job(self, name, **kwargs):
            captured.append(kwargs.get("credential_context"))

    async def _pool():
        return _Pool()

    monkeypatch.setattr(qs, "get_arq_pool", _pool)
    token = bind_credential_context({"orgId": "org-2"})
    try:
        await qs.queue_service.enqueue_agent_message(
            chat_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()), user_token="t",
            content="hi", channel_id=str(uuid.uuid4()),
        )
    finally:
        reset_credential_context(token)
    assert captured == [{"orgId": "org-2"}]

    seen = []

    @agent_jobs._acting_as_job_key
    async def job(ctx, **kwargs):
        seen.append(current_credential_context())

    await job({}, credential_context={"orgId": "org-2"})
    assert seen == [{"orgId": "org-2"}]

    async def stored(api_key_id):
        return {"orgId": "org-7"}

    monkeypatch.setattr(agent_jobs, "_stored_key_context", stored)
    await job({}, api_key_id=str(uuid.uuid4()))
    assert seen[-1] == {"orgId": "org-7"}
    await job({})
    assert seen[-1] is None


async def test_a_function_job_carries_the_requests_context(monkeypatch):
    """A function enqueued from an org-2 request runs with org-2 bound: the
    job binds the context it was enqueued with, and the executor mints the
    function's access token with the bound context (so chats the function
    starts through Sinas are attributed to org-2)."""
    from app.core.auth import bind_credential_context, reset_credential_context
    from app.queue import worker
    from app.services import execution_engine
    from app.services import queue_service as qs

    captured = {}

    class _Pool:
        async def enqueue_job(self, name, **kwargs):
            captured.update(kwargs)

    class _Redis:
        async def set(self, *a, **k):
            pass

        async def get(self, *a, **k):
            return None

    async def _pool():
        return _Pool()

    async def _redis():
        return _Redis()

    monkeypatch.setattr(qs, "get_arq_pool", _pool)
    monkeypatch.setattr(qs, "get_redis", _redis)
    token = bind_credential_context({"orgId": "org-2"})
    try:
        await qs.queue_service.enqueue_function("ns", "fn", {}, str(uuid.uuid4()), "api", "t", str(uuid.uuid4()))
    finally:
        reset_credential_context(token)
    assert captured["credential_context"] == {"orgId": "org-2"}

    class _Stop(Exception):
        pass

    seen = []

    async def execute_function(*args, **kwargs):
        seen.append(current_credential_context())
        raise _Stop

    monkeypatch.setattr(execution_engine.executor, "execute_function", execute_function)
    job_kwargs = {k: v for k, v in captured.items() if not k.startswith("_")}
    try:
        await worker.execute_function_job({"redis": _Redis()}, **job_kwargs)
    except Exception:
        pass
    assert seen == [{"orgId": "org-2"}]

