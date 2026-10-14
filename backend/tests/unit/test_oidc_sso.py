"""SSO login via OpenID Connect (docs/design/oidc-sso.md).

The identity provider is an httpx MockTransport: discovery, a JWKS with a
key generated here, a token endpoint minting ID tokens signed with it, and
userinfo. The flow runs through the real endpoints: /start, the callback,
the one-time code exchange.
"""
import base64
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.config_revision import ConfigRevision
from app.models.identity_provider import IdentityProvider
from app.models.secret import Secret
from app.models.user import OTPSession, RefreshToken, Role, User, UserIdentity, UserRole
from app.schemas.config import SinasConfig
from app.services import sso
from app.services.config_apply.service import ConfigApplyService
from app.services.config_export import ConfigExportService
from tests.conftest import auth_headers

ISSUER = "https://idp.example.com/realms/acme"
CLIENT_ID = "sinas-console"
CLIENT_SECRET = "s3cr3t-for-tests-only"


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _b64(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class SigningKey:
    def __init__(self, kid: str):
        self.kid = kid
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = self.key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        numbers = self.key.public_key().public_numbers()
        self.jwk = {"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256", "n": _b64(numbers.n), "e": _b64(numbers.e)}

    def sign(self, claims: dict) -> str:
        return jwt.encode(claims, self.pem, algorithm="RS256", headers={"kid": self.kid})


class FakeIdP:
    """One OIDC provider. `grant(code, claims)` decides what a code yields;
    the nonce comes from the authorization request, as a real IdP does."""

    def __init__(self):
        self.key = SigningKey("k1")
        self.published = [self.key]
        self.grants: dict[str, dict] = {}
        self.userinfo: dict | None = None
        self.token_requests: list[httpx.Request] = []
        self.overrides: dict = {}  # claims to force onto the next ID token
        self.signer: SigningKey | None = None  # sign with this instead
        self.token_status = 200

    def grant(self, code: str, claims: dict, nonce: str) -> None:
        self.grants[code] = {**claims, "nonce": nonce}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
                "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
                "userinfo_endpoint": f"{ISSUER}/protocol/openid-connect/userinfo",
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            })
        if url == f"{ISSUER}/protocol/openid-connect/certs":
            return httpx.Response(200, json={"keys": [k.jwk for k in self.published]})
        if url == f"{ISSUER}/protocol/openid-connect/token":
            self.token_requests.append(request)
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            form = parse_qs(request.content.decode())
            grant = self.grants.pop(form["code"][0], None)
            if grant is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            now = int(time.time())
            claims = {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300, **grant, **self.overrides}
            return httpx.Response(200, json={
                "access_token": "at-" + uuid.uuid4().hex, "token_type": "Bearer",
                "id_token": (self.signer or self.key).sign(claims),
            })
        if url == f"{ISSUER}/protocol/openid-connect/userinfo":
            if self.userinfo is None:
                return httpx.Response(404)
            return httpx.Response(200, json=self.userinfo)
        return httpx.Response(404)


@pytest.fixture
def idp(monkeypatch):
    fake = FakeIdP()
    real = httpx.AsyncClient
    monkeypatch.setattr(sso, "_client", lambda: real(transport=httpx.MockTransport(fake.handler)))
    sso.clear_caches()
    yield fake
    sso.clear_caches()


async def _role(db: AsyncSession, name: str | None = None) -> Role:
    role = Role(name=name or f"role-{_uid()}")
    db.add(role)
    await db.flush()
    return role


async def _provider(db: AsyncSession, **fields) -> IdentityProvider:
    secret_name = f"IDP_SECRET_{_uid()}"
    from app.core.encryption import encryption_service

    owner = User(email=f"owner-{_uid()}@example.com")
    db.add(owner)
    await db.flush()
    db.add(Secret(user_id=owner.id, name=secret_name, encrypted_value=encryption_service.encrypt(CLIENT_SECRET)))
    provider = IdentityProvider(
        name=f"kc-{_uid()}", display_name="Acme SSO", issuer=ISSUER, client_id=CLIENT_ID,
        client_secret=secret_name, scopes=["openid", "email", "profile"], email_claim="email",
        groups_claim="groups", role_mappings=[], allowed_domains=[], enforce_for_domains=[],
        auto_provision=True, session_hours=12, is_active=True,
    )
    for key, value in fields.items():
        setattr(provider, key, value)
    db.add(provider)
    await db.flush()
    return provider


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(uuid.uuid4().bytes * 3).decode().rstrip("=")
    return verifier, sso.pkce_challenge(verifier)


async def _start(client, provider, challenge: str, return_to: str = "/agents") -> dict:
    r = await client.get(f"/auth/oidc/{provider.name}/start", params={"code_challenge": challenge, "return_to": return_to})
    assert r.status_code == 302, r.text
    location = r.headers["location"]
    assert location.startswith(f"{ISSUER}/protocol/openid-connect/auth?"), location
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def _console_params(response) -> dict:
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(sso.console_callback_url() + "?"), location
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


async def _callback(client, provider, idp: FakeIdP, claims: dict, *, start: dict | None = None, challenge=None):
    """Through the IdP and back; the console's query parameters."""
    if start is None:
        start = await _start(client, provider, challenge or _pkce()[1])
    code = "c-" + uuid.uuid4().hex
    idp.grant(code, claims, start["nonce"])
    r = await client.get(f"/auth/oidc/{provider.name}/callback", params={"code": code, "state": start["state"]})
    return _console_params(r)


async def _login(client, provider, idp, claims: dict) -> dict:
    """The whole flow; the exchange's response body."""
    verifier, challenge = _pkce()
    params = await _callback(client, provider, idp, claims, challenge=challenge)
    assert "code" in params, params
    r = await client.post("/auth/oidc/exchange", json={"code": params["code"], "code_verifier": verifier})
    assert r.status_code == 200, r.text
    return r.json()


def _claims(email: str, *, sub: str | None = None, verified=True, groups=None, **extra) -> dict:
    claims = {"sub": sub or "sub-" + _uid(), "email": email, **extra}
    if verified is not None:
        claims["email_verified"] = verified
    if groups is not None:
        claims["groups"] = groups
    return claims


async def _memberships(db: AsyncSession, user_id) -> dict[str, UserRole]:
    rows = (
        await db.execute(
            select(UserRole, Role.name).join(Role, Role.id == UserRole.role_id).where(UserRole.user_id == user_id)
        )
    ).all()
    return {name: m for m, name in rows}


# ---------------------------------------------------------------- pure parts


class TestClaims:
    CLAIMS = {
        "groups": ["a", "b", 3],
        "realm_access": {"roles": ["admin"]},
        "https://example.com/roles": ["editor"],
        "https://example.com/claims": {"teams": "red"},
        "single": "solo",
    }

    @pytest.mark.parametrize("path, values", [
        ("groups", ["a", "b"]),
        ("realm_access.roles", ["admin"]),
        ("https://example.com/roles", ["editor"]),
        ("https://example.com/claims.teams", ["red"]),
        ("single", ["solo"]),
        ("missing", None),
        ("realm_access.missing", None),
    ])
    def test_paths(self, path, values):
        assert sso.claim_values(self.CLAIMS, path) == values

    @pytest.mark.parametrize("value, safe", [
        ("/agents", "/agents"), ("/a?b=c", "/a?b=c"), ("https://evil.example", "/"),
        ("//evil.example/x", "/"), ("/\\evil.example", "/"), ("", "/"), (None, "/"), ("agents", "/"),
    ])
    def test_return_to_stays_in_the_console(self, value, safe):
        assert sso.safe_return_to(value) == safe

    def test_email_verified_as_the_idp_says(self):
        assert sso.email_verified({"email_verified": True}) is True
        assert sso.email_verified({"email_verified": "false"}) is False
        assert sso.email_verified({}) is None


# ---------------------------------------------------------------- the flow


class TestLogin:
    async def test_happy_path_provisions_the_user_with_mapped_roles(self, client, db, idp):
        admins, readers = await _role(db), await _role(db)
        provider = await _provider(db, role_mappings=[
            {"group": "sinas-admins", "roles": [admins.name]},
            {"group": "nobody", "roles": [readers.name]},
        ])
        email = f"ann-{_uid()}@acme.com"
        claims = _claims(email, groups=["sinas-admins", "other"])
        body = await _login(client, provider, idp, claims)

        assert body["user"]["email"] == email and admins.name in body["user"]["roles"]
        assert body["return_to"] == "/agents"
        user = (await db.execute(select(User).where(User.email == email))).scalar_one()
        identity = (await db.execute(select(UserIdentity).where(UserIdentity.user_id == user.id))).scalar_one()
        assert (identity.provider, identity.subject) == (ISSUER, claims["sub"])
        roles = await _memberships(db, user.id)
        assert set(roles) == {admins.name} and roles[admins.name].source == f"idp:{provider.name}"
        # The confidential client authenticated with the Secret's value, PKCE included.
        request = idp.token_requests[-1]
        assert request.headers["authorization"].startswith("Basic ")
        assert "code_verifier=" in request.content.decode()
        # The tokens work.
        me = await client.get("/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
        assert me.status_code == 200 and me.json()["email"] == email

    async def test_a_second_login_finds_the_user_by_subject(self, client, db, idp):
        provider = await _provider(db)
        claims = _claims(f"bo-{_uid()}@acme.com")
        first = await _login(client, provider, idp, claims)
        # The IdP changed the address: same person, same subject.
        again = await _login(client, provider, idp, {**claims, "email": f"bo2-{_uid()}@acme.com"})
        assert again["user"]["id"] == first["user"]["id"]

    async def test_links_an_existing_account_by_verified_email(self, client, db, idp):
        provider = await _provider(db, auto_provision=False)
        user = User(email=f"cy-{_uid()}@acme.com")
        db.add(user)
        await db.flush()
        body = await _login(client, provider, idp, _claims(user.email, verified=True))
        assert body["user"]["id"] == str(user.id)
        assert (await db.execute(select(UserIdentity).where(UserIdentity.user_id == user.id))).scalar_one()

    @pytest.mark.parametrize("verified", [False, None, "false"])
    async def test_refuses_linking_by_an_unverified_email(self, client, db, idp, verified):
        provider = await _provider(db)
        user = User(email=f"dee-{_uid()}@acme.com")
        db.add(user)
        await db.flush()
        params = await _callback(client, provider, idp, _claims(user.email, verified=verified))
        assert "code" not in params and "verified" in params["error"]
        assert (await db.execute(select(UserIdentity).where(UserIdentity.user_id == user.id))).first() is None

    async def test_never_links_the_superadmin_by_email(self, client, db, idp, monkeypatch):
        provider = await _provider(db)
        user = User(email=f"root-{_uid()}@acme.com")
        db.add(user)
        await db.flush()
        monkeypatch.setattr(settings, "superadmin_email", user.email)
        params = await _callback(client, provider, idp, _claims(user.email))
        assert "superadmin" in params["error"]

    async def test_never_links_onto_an_account_holding_another_subject(self, client, db, idp):
        provider = await _provider(db)
        email = f"eve-{_uid()}@acme.com"
        await _login(client, provider, idp, _claims(email, sub="first"))
        params = await _callback(client, provider, idp, _claims(email, sub="second"))
        assert "already linked" in params["error"]

    async def test_unknown_users_need_auto_provision(self, client, db, idp):
        provider = await _provider(db, auto_provision=False)
        email = f"fay-{_uid()}@acme.com"
        params = await _callback(client, provider, idp, _claims(email))
        assert "no Sinas account" in params["error"]
        assert (await db.execute(select(User).where(User.email == email))).first() is None

    async def test_an_unverified_email_provisions_nothing(self, client, db, idp):
        provider = await _provider(db)
        params = await _callback(client, provider, idp, _claims(f"gus-{_uid()}@acme.com", verified=False))
        assert "verified" in params["error"]

    async def test_default_role_is_given_once(self, client, db, idp):
        guests = await _role(db)
        provider = await _provider(db, default_role=guests.name)
        claims = _claims(f"hal-{_uid()}@acme.com")
        body = await _login(client, provider, idp, claims)
        roles = await _memberships(db, uuid.UUID(body["user"]["id"]))
        assert roles[guests.name].active and roles[guests.name].source is None
        await _login(client, provider, idp, claims)  # not mapped: still kept
        assert (await _memberships(db, uuid.UUID(body["user"]["id"])))[guests.name].active

    @pytest.mark.parametrize("email, verified", [("ivy@other.com", True), ("ivy@acme.com", False)])
    async def test_allowed_domains(self, client, db, idp, email, verified):
        provider = await _provider(db, allowed_domains=["acme.com"])
        params = await _callback(client, provider, idp, _claims(email.replace("@", f"-{_uid()}@"), verified=verified))
        assert "error" in params and "code" not in params

    async def test_allowed_domains_apply_to_linked_users_too(self, client, db, idp):
        provider = await _provider(db)
        claims = _claims(f"jo-{_uid()}@acme.com")
        await _login(client, provider, idp, claims)
        provider.allowed_domains = ["elsewhere.com"]
        await db.flush()
        params = await _callback(client, provider, idp, claims)
        assert "isn't open to your email domain" in params["error"]

    async def test_userinfo_fills_in_groups(self, client, db, idp):
        team = await _role(db)
        provider = await _provider(db, role_mappings=[{"group": "team", "roles": [team.name]}])
        claims = _claims(f"kim-{_uid()}@acme.com")
        idp.userinfo = {"sub": claims["sub"], "groups": ["team"]}
        body = await _login(client, provider, idp, claims)
        assert team.name in body["user"]["roles"]

    async def test_userinfo_for_another_subject_is_refused(self, client, db, idp):
        provider = await _provider(db, role_mappings=[{"group": "team", "roles": [(await _role(db)).name]}])
        idp.userinfo = {"sub": "someone-else", "groups": ["team"]}
        params = await _callback(client, provider, idp, _claims(f"lu-{_uid()}@acme.com"))
        assert "someone else" in params["error"]

    async def test_inactive_providers_cant_be_used(self, client, db, idp):
        provider = await _provider(db, is_active=False)
        r = await client.get(f"/auth/oidc/{provider.name}/start", params={"code_challenge": _pkce()[1]})
        assert "error" in _console_params(r)
        listed = (await client.get("/auth/oidc/providers")).json()
        assert provider.name not in [p["name"] for p in listed]

    async def test_the_public_list_shows_names_only(self, client, db, idp):
        provider = await _provider(db, enforce_for_domains=["acme.com"])
        listed = {p["name"]: p for p in (await client.get("/auth/oidc/providers")).json()}
        assert listed[provider.name] == {"name": provider.name, "display_name": "Acme SSO"}


class TestRefusals:
    async def test_unknown_state(self, client, db, idp):
        provider = await _provider(db)
        r = await client.get(f"/auth/oidc/{provider.name}/callback", params={"code": "x", "state": "forged"})
        assert "expired" in _console_params(r)["error"]
        assert not idp.token_requests

    async def test_state_is_single_use(self, client, db, idp):
        provider = await _provider(db)
        start = await _start(client, provider, _pkce()[1])
        await _callback(client, provider, idp, _claims(f"mo-{_uid()}@acme.com"), start=start)
        again = await client.get(f"/auth/oidc/{provider.name}/callback", params={"code": "c2", "state": start["state"]})
        assert "expired" in _console_params(again)["error"]

    async def test_state_of_another_browser(self, client, db, idp):
        provider = await _provider(db)
        start = await _start(client, provider, _pkce()[1])
        client.cookies.clear()  # the callback arrives in a browser that didn't start it
        params = await _callback(client, provider, idp, _claims(f"ned-{_uid()}@acme.com"), start=start)
        assert "another browser" in params["error"]
        assert not idp.token_requests

    async def test_a_start_without_a_challenge_is_refused(self, client, db, idp):
        provider = await _provider(db)
        r = await client.get(f"/auth/oidc/{provider.name}/start")
        assert "incomplete" in _console_params(r)["error"]

    @pytest.mark.parametrize("override, message", [
        ({"nonce": "not-the-one"}, "nonce"),
        ({"aud": "another-client"}, "isn't meant for this Sinas"),
        ({"iss": "https://evil.example.com"}, "isn't meant for this Sinas"),
        ({"exp": int(time.time()) - 600, "iat": int(time.time()) - 900}, "expired"),
        ({"aud": [CLIENT_ID, "another"], "azp": "another"}, "another client"),
    ])
    async def test_bad_id_tokens(self, client, db, idp, override, message):
        provider = await _provider(db)
        idp.overrides = override
        email = f"oz-{_uid()}@acme.com"
        params = await _callback(client, provider, idp, _claims(email))
        assert message in params["error"]
        assert (await db.execute(select(User).where(User.email == email))).first() is None

    async def test_a_token_within_the_clock_skew_passes(self, client, db, idp):
        provider = await _provider(db)
        idp.overrides = {"exp": int(time.time()) - 20}
        await _login(client, provider, idp, _claims(f"pia-{_uid()}@acme.com"))

    async def test_a_signature_by_an_unpublished_key(self, client, db, idp):
        provider = await _provider(db)
        idp.signer = SigningKey("k1")  # same kid, other key
        params = await _callback(client, provider, idp, _claims(f"quin-{_uid()}@acme.com"))
        assert "could not be verified" in params["error"]

    async def test_a_rotated_key_is_fetched_again(self, client, db, idp):
        provider = await _provider(db)
        await _login(client, provider, idp, _claims(f"rae-{_uid()}@acme.com"))  # caches k1
        # A minute later (sooner, an unknown kid doesn't refetch).
        for uri, (fetched, keys) in list(sso._jwks_cache.items()):
            sso._jwks_cache[uri] = (fetched - 120, keys)
        idp.signer = SigningKey("k2")
        idp.published = [idp.key, idp.signer]
        await _login(client, provider, idp, _claims(f"rae2-{_uid()}@acme.com"))

    async def test_an_hmac_signed_token_is_refused(self, client, db, idp):
        provider = await _provider(db)

        class Hmac(SigningKey):
            def sign(self, claims):
                return jwt.encode(claims, CLIENT_SECRET, algorithm="HS256", headers={"kid": "k1"})

        idp.signer = Hmac("k1")
        params = await _callback(client, provider, idp, _claims(f"sal-{_uid()}@acme.com"))
        assert "algorithm" in params["error"]

    async def test_the_idps_error_is_reduced_to_its_code(self, client, db, idp):
        provider = await _provider(db)
        start = await _start(client, provider, _pkce()[1])
        r = await client.get(f"/auth/oidc/{provider.name}/callback", params={
            "state": start["state"], "error": "access_denied", "error_description": "Call +1 555 to fix",
        })
        error = _console_params(r)["error"]
        assert "access_denied" in error and "555" not in error

    async def test_a_missing_client_secret(self, client, db, idp):
        provider = await _provider(db, client_secret="NOT_THERE")
        params = await _callback(client, provider, idp, _claims(f"tia-{_uid()}@acme.com"))
        assert "client secret" in params["error"]


class TestOneTimeCode:
    async def test_single_use(self, client, db, idp):
        provider = await _provider(db)
        verifier, challenge = _pkce()
        params = await _callback(client, provider, idp, _claims(f"uma-{_uid()}@acme.com"), challenge=challenge)
        body = {"code": params["code"], "code_verifier": verifier}
        assert (await client.post("/auth/oidc/exchange", json=body)).status_code == 200
        assert (await client.post("/auth/oidc/exchange", json=body)).status_code == 400

    async def test_needs_the_consoles_verifier(self, client, db, idp):
        provider = await _provider(db)
        _, challenge = _pkce()
        params = await _callback(client, provider, idp, _claims(f"val-{_uid()}@acme.com"), challenge=challenge)
        other, _ = _pkce()
        r = await client.post("/auth/oidc/exchange", json={"code": params["code"], "code_verifier": other})
        assert r.status_code == 400
        # And the code is spent: guessing again doesn't help.
        assert (await client.post("/auth/oidc/exchange", json={"code": params["code"], "code_verifier": other})).status_code == 400

    async def test_tokens_are_never_in_the_redirect(self, client, db, idp):
        provider = await _provider(db)
        params = await _callback(client, provider, idp, _claims(f"wes-{_uid()}@acme.com"))
        assert set(params) == {"code"}


class TestRoleSync:
    async def test_adds_and_removes_only_its_own_memberships(self, client, db, idp):
        mapped, manual, other_idp, both = await _role(db), await _role(db), await _role(db), await _role(db)
        provider = await _provider(db, role_mappings=[
            {"group": "g-mapped", "roles": [mapped.name]},
            {"group": "g-manual", "roles": [manual.name]},
            {"group": "g-both", "roles": [both.name]},
        ])
        claims = _claims(f"xia-{_uid()}@acme.com", groups=["g-mapped", "g-both"])
        user_id = uuid.UUID((await _login(client, provider, idp, claims))["user"]["id"])

        # By hand, and by another provider; "both" was granted by this one.
        db.add(UserRole(user_id=user_id, role_id=manual.id, active=True))
        db.add(UserRole(user_id=user_id, role_id=other_idp.id, active=True, source="idp:another"))
        await db.flush()
        roles = await _memberships(db, user_id)
        assert roles[mapped.name].source == roles[both.name].source == f"idp:{provider.name}"

        # The groups change: g-mapped and g-both are gone, g-manual appears.
        await _login(client, provider, idp, {**claims, "groups": ["g-manual"]})
        roles = await _memberships(db, user_id)
        assert not roles[mapped.name].active and roles[mapped.name].removed_at is not None
        assert not roles[both.name].active
        # Held by hand already: left as it is, not taken over.
        assert roles[manual.name].active and roles[manual.name].source is None
        assert roles[other_idp.name].active and roles[other_idp.name].source == "idp:another"

        # The group comes back: the ended membership is reactivated, as the provider's.
        await _login(client, provider, idp, {**claims, "groups": []})
        await _login(client, provider, idp, {**claims, "groups": ["g-mapped"]})
        roles = await _memberships(db, user_id)
        assert roles[mapped.name].active and roles[mapped.name].source == f"idp:{provider.name}"
        # Manual memberships survive a login that maps nothing.
        assert roles[manual.name].active
        rows = (await db.execute(
            select(UserRole).where(UserRole.user_id == user_id, UserRole.role_id == mapped.id)
        )).scalars().all()
        assert len(rows) == 1  # reactivated, not duplicated

    async def test_email_domain_rules(self, client, db, idp):
        staff = await _role(db)
        provider = await _provider(db, role_mappings=[{"email_domain": "acme.com", "roles": [staff.name]}])
        body = await _login(client, provider, idp, _claims(f"yan-{_uid()}@acme.com"))
        assert staff.name in body["user"]["roles"]
        other = await _login(client, provider, idp, _claims(f"yan-{_uid()}@else.com"))
        assert staff.name not in other["user"]["roles"]

    async def test_a_manual_grant_of_a_provider_membership_is_kept(self, client, db, idp, admin_user):
        """An admin re-adding a membership the provider ended makes it theirs."""
        team = await _role(db)
        provider = await _provider(db, role_mappings=[{"group": "team", "roles": [team.name]}])
        claims = _claims(f"zed-{_uid()}@acme.com", groups=["team"])
        user_id = (await _login(client, provider, idp, claims))["user"]["id"]
        await _login(client, provider, idp, {**claims, "groups": []})
        r = await client.post(f"/api/v1/roles/{team.name}/members", json={"user_id": user_id}, headers=auth_headers(admin_user))
        assert r.status_code == 200, r.text
        await _login(client, provider, idp, {**claims, "groups": []})
        assert (await _memberships(db, uuid.UUID(user_id)))[team.name].active


class TestSessions:
    async def _refresh_row(self, db, plain: str) -> RefreshToken:
        import hashlib

        token_hash = hashlib.sha256(plain.encode()).hexdigest()
        return (await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))).scalar_one()

    async def test_refresh_token_lasts_session_hours(self, client, db, idp):
        provider = await _provider(db, session_hours=3)
        body = await _login(client, provider, idp, _claims(f"abe-{_uid()}@acme.com"))
        row = await self._refresh_row(db, body["refresh_token"])
        left = row.expires_at - datetime.now(UTC)
        assert timedelta(hours=2, minutes=58) < left <= timedelta(hours=3)
        assert row.identity_provider == provider.name

    async def test_never_longer_than_refresh_token_expire_days(self, client, db, idp):
        provider = await _provider(db, session_hours=24 * 90)
        body = await _login(client, provider, idp, _claims(f"bea-{_uid()}@acme.com"))
        row = await self._refresh_row(db, body["refresh_token"])
        assert row.expires_at - datetime.now(UTC) <= timedelta(days=settings.refresh_token_expire_days)

    async def test_an_expired_sso_session_cant_refresh(self, client, db, idp):
        provider = await _provider(db, session_hours=1)
        body = await _login(client, provider, idp, _claims(f"cal-{_uid()}@acme.com"))
        row = await self._refresh_row(db, body["refresh_token"])
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.flush()
        r = await client.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
        assert r.status_code == 401


class TestEnforcement:
    async def test_password_and_otp_login_refused_for_an_enforced_domain(self, client, db, idp):
        provider = await _provider(db, enforce_for_domains=["acme.com"])
        email = f"dot-{_uid()}@acme.com"
        db.add(User(email=email))
        await db.flush()
        r = await client.post("/auth/login", json={"email": email, "password": "whatever"})
        assert r.status_code == 403 and "Acme SSO" in r.json()["detail"]
        # Unknown addresses at that domain get the same answer: nothing to enumerate.
        r = await client.post("/auth/login", json={"email": f"nobody-{_uid()}@acme.com"})
        assert r.status_code == 403
        # An OTP sent before enforcement no longer verifies.
        otp = OTPSession(email=email, otp_code="123456", expires_at=datetime.now(UTC) + timedelta(minutes=5))
        db.add(otp)
        await db.flush()
        r = await client.post("/auth/verify-otp", json={"session_id": str(otp.id), "otp_code": "123456"})
        assert r.status_code == 403
        # The console asks first and goes straight to SSO.
        r = await client.post("/auth/oidc/discover", json={"email": email})
        assert r.json()["provider"]["name"] == provider.name

    async def test_other_domains_are_unaffected(self, client, db, idp):
        await _provider(db, enforce_for_domains=["acme.com"])
        r = await client.post("/auth/login", json={"email": f"eli-{_uid()}@other.com"})
        assert r.status_code == 200
        assert (await client.post("/auth/oidc/discover", json={"email": "x@other.com"})).json()["provider"] is None

    async def test_the_superadmin_is_exempt(self, client, db, idp, monkeypatch):
        await _provider(db, enforce_for_domains=["acme.com"])
        email = f"root-{_uid()}@acme.com"
        monkeypatch.setattr(settings, "superadmin_email", email.upper())
        r = await client.post("/auth/login", json={"email": email})
        assert r.status_code == 200  # the usual OTP answer
        assert (await client.post("/auth/oidc/discover", json={"email": email})).json()["provider"] is None

    async def test_a_password_session_ends_but_an_sso_one_continues(self, client, db, idp):
        from app.core.auth import create_refresh_token

        provider = await _provider(db)
        email = f"fin-{_uid()}@acme.com"
        sso_body = await _login(client, provider, idp, _claims(email))
        user = (await db.execute(select(User).where(User.email == email))).scalar_one()
        password_token, _ = await create_refresh_token(db, str(user.id))
        assert (await client.post("/auth/refresh", json={"refresh_token": password_token})).status_code == 200

        provider.enforce_for_domains = ["acme.com"]
        await db.flush()
        r = await client.post("/auth/refresh", json={"refresh_token": password_token})
        assert r.status_code == 401 and "sign in with" in r.json()["detail"]
        assert (await client.post("/auth/refresh", json={"refresh_token": sso_body["refresh_token"]})).status_code == 200

    async def test_an_inactive_provider_enforces_nothing(self, client, db, idp):
        await _provider(db, enforce_for_domains=["acme.com"], is_active=False)
        r = await client.post("/auth/login", json={"email": f"gil-{_uid()}@acme.com"})
        assert r.status_code == 200


# ---------------------------------------------------------------- the resource kind


def _config(spec: dict) -> SinasConfig:
    return SinasConfig.model_validate({
        "apiVersion": "sinas.co/v1", "kind": "SinasConfig", "metadata": {"name": "cfg"}, "spec": spec,
    })


def _yaml_provider(name: str, role: str, **extra) -> dict:
    return {
        "name": name, "displayName": "Keycloak", "issuer": ISSUER, "clientId": CLIENT_ID,
        "clientSecret": "KEYCLOAK_CLIENT_SECRET", "groupsClaim": "realm_access.roles",
        "roleMappings": [{"group": "sinas-admins", "roles": [role]}, {"emailDomain": "acme.com", "roles": [role]}],
        "allowedDomains": ["acme.com"], "autoProvision": True, "enforceForDomains": ["acme.com"],
        "sessionHours": 8, **extra,
    }


class TestResourceKind:
    async def test_config_apply_export_round_trip_and_history(self, db, admin_user):
        role = f"role-{_uid()}"
        name = f"kc-{_uid()}"
        declared = _yaml_provider(name, role)
        svc = ConfigApplyService(db, "cfg", owner_user_id=str(admin_user.id), auto_commit=False)
        result = await svc.apply_config(_config({"roles": [{"name": role}], "identityProviders": [declared]}))
        assert result.success, result.errors
        row = (await db.execute(select(IdentityProvider).where(IdentityProvider.name == name))).scalar_one()
        assert (row.groups_claim, row.session_hours, row.managed_by) == ("realm_access.roles", 8, "config")
        assert row.role_mappings[1]["email_domain"] == "acme.com"

        exported = yaml.safe_load(await ConfigExportService(db).export_config())
        mine = next(p for p in exported["spec"]["identityProviders"] if p["name"] == name)
        assert mine == {**declared, "scopes": ["openid", "email", "profile"], "emailClaim": "email", "isActive": True}

        # Re-applying the export changes nothing.
        svc = ConfigApplyService(db, "cfg", owner_user_id=str(admin_user.id), auto_commit=False)
        again = await svc.apply_config(_config({"identityProviders": [mine]}))
        assert again.success and again.summary.unchanged.get("identityProviders") == 1

        revisions = (await db.execute(
            select(ConfigRevision).where(ConfigRevision.resource_kind == "identityProviders", ConfigRevision.resource_key == name)
        )).scalars().all()
        assert [r.action for r in revisions] == ["create"]
        assert revisions[0].spec["client_secret"] == "KEYCLOAK_CLIENT_SECRET"  # a name, not a value

    async def test_dry_run_accepts_roles_declared_alongside(self, db, admin_user):
        role = f"role-{_uid()}"
        svc = ConfigApplyService(db, "cfg", owner_user_id=str(admin_user.id), auto_commit=False)
        result = await svc.apply_config(
            _config({"roles": [{"name": role}], "identityProviders": [_yaml_provider(f"kc-{_uid()}", role)]}),
            dry_run=True,
        )
        assert result.success, result.errors

    async def test_unknown_roles_are_refused(self, db, admin_user):
        svc = ConfigApplyService(db, "cfg", owner_user_id=str(admin_user.id), auto_commit=False)
        result = await svc.apply_config(_config({"identityProviders": [_yaml_provider(f"kc-{_uid()}", "NoSuchRole")]}))
        assert not result.success and "NoSuchRole" in result.errors[0]

    @pytest.mark.parametrize("bad", [
        {"issuer": "http://idp.example.com"},  # plain http off localhost
        {"issuer": "https://user:pw@idp.example.com"},
        {"scopes": ["email"]},  # no openid
        {"roleMappings": [{"roles": ["X"]}]},  # no matcher
        {"roleMappings": [{"group": "g", "emailDomain": "acme.com", "roles": ["X"]}]},  # two
        {"roleMappings": [{"group": "g", "roles": []}]},
        {"allowedDomains": ["not a domain"]},
        {"sessionHours": 0},
        {"name": "Has Spaces"},
        {"clientSecrett": "typo"},
    ])
    async def test_refuses_what_cannot_work(self, db, admin_user, bad):
        svc = ConfigApplyService(db, "cfg", owner_user_id=str(admin_user.id), auto_commit=False)
        try:
            config = _config({"identityProviders": [{**_yaml_provider(f"kc-{_uid()}", "Admins"), **bad}]})
        except Exception:
            return  # refused by the config schema already
        result = await svc.apply_config(config)
        assert not result.success

    async def test_packages_cannot_declare_identity_providers(self):
        from app.services.package_service import PACKAGE_SKIP_TYPES

        assert "identityProviders" in PACKAGE_SKIP_TYPES

    async def test_rest_crud_with_history_and_restore(self, client, db, admin_user, test_user):
        role = await _role(db)
        h = auth_headers(admin_user)
        name = f"kc-{_uid()}"
        r = await client.post("/api/v1/identity-providers", headers=h, json={
            "name": name, "display_name": "Keycloak", "issuer": ISSUER, "client_id": CLIENT_ID,
            "client_secret": "KC_SECRET", "role_mappings": [{"group": "g", "roles": [role.name]}],
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["redirect_uri"] == f"{settings.public_base_url}/auth/oidc/{name}/callback"
        assert body["scopes"] == ["openid", "email", "profile"] and body["session_hours"] == 12

        r = await client.put(f"/api/v1/identity-providers/{name}", headers=h, json={"client_secret": "", "session_hours": 4})
        assert r.status_code == 200 and r.json()["client_secret"] is None and r.json()["session_hours"] == 4
        r = await client.put(f"/api/v1/identity-providers/{name}", headers=h, json={"name": "renamed"})
        assert r.status_code == 422
        r = await client.put(f"/api/v1/identity-providers/{name}", headers=h, json={
            "role_mappings": [{"group": "g", "roles": ["NoSuchRole"]}],
        })
        assert r.status_code == 404

        assert (await client.get("/api/v1/identity-providers", headers=auth_headers(test_user))).status_code == 403
        assert (await client.delete(f"/api/v1/identity-providers/{name}", headers=h)).status_code == 204

        revisions = (await db.execute(
            select(ConfigRevision).where(ConfigRevision.resource_kind == "identityProviders", ConfigRevision.resource_key == name)
            .order_by(ConfigRevision.id)
        )).scalars().all()
        assert [r.action for r in revisions] == ["create", "update", "delete"]
        r = await client.post(f"/api/v1/config/history/{revisions[1].id}/restore", headers=h)
        assert r.status_code == 200, r.text
        restored = (await db.execute(select(IdentityProvider).where(IdentityProvider.name == name))).scalar_one()
        assert restored.session_hours == 4
