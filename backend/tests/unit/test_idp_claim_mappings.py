"""Identity provider claim mappings: claims copied into custom fields at login.

Pinned here: a mapped field is set from its claim at every login, updated
when the claim changes and removed when the claim is gone; fields no mapping
names (set by hand or by token exchange) are never touched; a field whose
mapping was removed is cleared at the next login unless someone changed it
since; claim paths reach nested and dotted (namespaced) claims, userinfo
included; field names are validated; config apply/export round-trips them.
"""
import uuid

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import select

from app.models.identity_provider import IdentityProvider
from app.models.user import User, UserIdentity
from app.schemas.spec.identity_provider import IdentityProviderSpec
from app.services import sso
from app.services.config_apply.service import ConfigApplyService
from app.services.config_export import ConfigExportService
from tests.unit.test_oidc_sso import (  # noqa: F401 - the idp fixture
    ISSUER,
    _claims,
    _config,
    _login,
    _provider,
    _uid,
    _yaml_provider,
    idp,
)


async def _fields(db, user_id) -> dict:
    user = (await db.execute(select(User).where(User.id == uuid.UUID(str(user_id))))).scalar_one()
    await db.refresh(user)
    return dict(user.custom_fields or {})


# ── pure parts ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "claims, path, expected",
    [
        ({"org_id": "org_01H"}, "org_id", "org_01H"),
        ({"tid": "72f988bf"}, "tid", "72f988bf"),
        ({"https://example.com/org_id": "acme"}, "https://example.com/org_id", "acme"),
        ({"organization": {"id": "o-1", "name": "Acme"}}, "organization.id", "o-1"),
        ({"organization": ["acme"]}, "organization", "acme"),  # one item: the item
        ({"teams": ["a", "b"]}, "teams", ["a", "b"]),
        ({"seats": 5, "beta": False}, "seats", 5),
        ({"beta": False}, "beta", False),
        ({"org_id": "  "}, "org_id", None),
        ({"org_id": "x" * 1025}, "org_id", None),
        ({"organization": {"id": "o-1"}}, "organization", None),  # an object isn't copied
        ({"teams": [{"id": 1}]}, "teams", None),
        ({}, "org_id", None),
        ({"org_id": None}, "org_id", None),
    ],
)
def test_mapped_claim_values(claims, path, expected):
    assert sso.mapped_claim_value(claims, path) == expected


def _idp(mappings):
    return IdentityProvider(name="idp", claim_mappings=mappings)


def test_sync_sets_updates_and_removes_only_mapped_fields():
    user = User(email="a@example.com", custom_fields={"orgId": "old", "department": "sales", "tier": "gold"})
    provider = _idp([{"claim": "org_id", "field": "orgId"}, {"claim": "plan", "field": "tier"}])

    now = sso.sync_custom_fields(user, provider, {"org_id": "org-2", "plan": "pro"})
    assert user.custom_fields == {"orgId": "org-2", "department": "sales", "tier": "pro"}
    assert now == {"orgId": "org-2", "tier": "pro"}

    # The plan claim is gone: its field goes with it; the hand-set field stays.
    now = sso.sync_custom_fields(user, provider, {"org_id": "org-2"}, previous=now)
    assert user.custom_fields == {"orgId": "org-2", "department": "sales"}
    assert now == {"orgId": "org-2"}


def test_a_field_whose_mapping_was_removed_is_cleared_unless_changed_since():
    user = User(email="a@example.com", custom_fields={"orgId": "org-2", "tier": "pro"})
    previous = {"orgId": "org-2", "tier": "pro"}
    sso.sync_custom_fields(user, _idp([]), {"org_id": "org-2"}, previous=previous)
    assert user.custom_fields == {}

    # Changed by someone else since this provider set it: theirs now.
    user = User(email="a@example.com", custom_fields={"orgId": "set-by-admin"})
    sso.sync_custom_fields(user, _idp([]), {}, previous={"orgId": "org-2"})
    assert user.custom_fields == {"orgId": "set-by-admin"}


def test_without_mappings_nothing_is_touched():
    user = User(email="a@example.com", custom_fields={"orgId": "x"})
    assert sso.sync_custom_fields(user, _idp([]), {"org_id": "y"}) == {}
    assert user.custom_fields == {"orgId": "x"}


def _spec(**extra):
    return IdentityProviderSpec(
        name="kc", display_name="KC", issuer=ISSUER, client_id="c", **extra
    )


def test_the_spec_validates_mappings():
    spec = _spec(claimMappings=[{"claim": " org_id ", "field": "orgId"}, {"claim": "https://example.com/org", "field": "org2"}])
    assert [(m.claim, m.field) for m in spec.claim_mappings] == [("org_id", "orgId"), ("https://example.com/org", "org2")]
    for bad in (
        [{"claim": "org_id", "field": "org-id"}],
        [{"claim": "org_id", "field": "1org"}],
        [{"claim": "org_id", "field": ""}],
        [{"claim": "", "field": "orgId"}],
        [{"claim": ".org", "field": "orgId"}],
        [{"claim": "org id", "field": "orgId"}],
        [{"claim": "a", "field": "orgId"}, {"claim": "b", "field": "orgId"}],
        [{"claim": "a", "field": "orgId", "extra": 1}],
    ):
        with pytest.raises(ValidationError):
            _spec(claimMappings=bad)


def test_providers_without_mappings_keep_their_checksum():
    # canonical() leaves the unset field out, so a config apply after the
    # upgrade reports existing providers unchanged.
    assert "claim_mappings" not in _spec().canonical()
    assert "claim_mappings" in _spec(claimMappings=[{"claim": "org_id", "field": "orgId"}]).canonical()


# ── through the login flow ──────────────────────────────────────────────────


class TestLogin:
    async def test_every_login_syncs_mapped_fields(self, client, db, idp):
        provider = await _provider(db, claim_mappings=[
            {"claim": "org_id", "field": "orgId"},
            {"claim": "https://example.com/plan", "field": "plan"},
        ])
        claims = _claims(f"ana-{_uid()}@acme.com", org_id="org-1", **{"https://example.com/plan": "pro"})
        user_id = (await _login(client, provider, idp, claims))["user"]["id"]
        assert await _fields(db, user_id) == {"orgId": "org-1", "plan": "pro"}

        # Set by hand (or by token exchange): never touched by the sync.
        user = await db.get(User, uuid.UUID(user_id))
        user.custom_fields = {**user.custom_fields, "department": "research"}
        await db.flush()

        # The org changes; the plan claim disappears (userinfo has none either).
        idp.userinfo = {"sub": claims["sub"]}
        await _login(client, provider, idp, {**{k: v for k, v in claims.items() if "plan" not in k}, "org_id": "org-2"})
        assert await _fields(db, user_id) == {"orgId": "org-2", "department": "research"}

        identity = (await db.execute(
            select(UserIdentity).where(UserIdentity.user_id == uuid.UUID(user_id))
        )).scalar_one()
        assert identity.identity_metadata["claim_fields"] == {"orgId": "org-2"}

    async def test_a_claim_only_userinfo_has_is_fetched(self, client, db, idp):
        provider = await _provider(db, claim_mappings=[{"claim": "organization.id", "field": "orgId"}])
        claims = _claims(f"bo-{_uid()}@acme.com")
        idp.userinfo = {"sub": claims["sub"], "organization": {"id": "org-9"}}
        user_id = (await _login(client, provider, idp, claims))["user"]["id"]
        assert await _fields(db, user_id) == {"orgId": "org-9"}

    async def test_removing_a_mapping_clears_its_field_at_the_next_login(self, client, db, idp):
        provider = await _provider(db, claim_mappings=[{"claim": "org_id", "field": "orgId"}])
        claims = _claims(f"cy-{_uid()}@acme.com", org_id="org-1")
        user_id = (await _login(client, provider, idp, claims))["user"]["id"]
        provider.claim_mappings = []
        await db.flush()
        await _login(client, provider, idp, claims)
        assert await _fields(db, user_id) == {}


async def test_config_apply_export_round_trip(db, admin_user):
    role = f"role-{_uid()}"
    name = f"kc-{_uid()}"
    mappings = [{"claim": "org_id", "field": "orgId"}, {"claim": "https://example.com/plan", "field": "plan"}]
    declared = _yaml_provider(name, role, claimMappings=mappings)
    svc = ConfigApplyService(db, "cfg", owner_user_id=str(admin_user.id), auto_commit=False)
    result = await svc.apply_config(_config({"roles": [{"name": role}], "identityProviders": [declared]}))
    assert result.success, result.errors
    row = (await db.execute(select(IdentityProvider).where(IdentityProvider.name == name))).scalar_one()
    assert row.claim_mappings == mappings

    exported = yaml.safe_load(await ConfigExportService(db).export_config())
    mine = next(p for p in exported["spec"]["identityProviders"] if p["name"] == name)
    assert mine["claimMappings"] == mappings

    svc = ConfigApplyService(db, "cfg", owner_user_id=str(admin_user.id), auto_commit=False)
    again = await svc.apply_config(_config({"identityProviders": [mine]}))
    assert again.success and again.summary.unchanged.get("identityProviders") == 1


async def test_the_rest_api_reads_and_writes_mappings(client, db, admin_user):
    from tests.conftest import auth_headers

    name = f"kc-{_uid()}"
    body = {
        "name": name, "display_name": "KC", "issuer": ISSUER, "client_id": "c",
        "claim_mappings": [{"claim": "tid", "field": "tenantId"}],
    }
    r = await client.post("/api/v1/identity-providers", json=body, headers=auth_headers(admin_user))
    assert r.status_code == 201, r.text
    assert [(m["claim"], m["field"]) for m in r.json()["claim_mappings"]] == [("tid", "tenantId")]

    r = await client.put(
        f"/api/v1/identity-providers/{name}", json={"claim_mappings": [{"claim": "tid", "field": "bad-name"}]},
        headers=auth_headers(admin_user),
    )
    assert r.status_code in (400, 422), r.text
    r = await client.put(f"/api/v1/identity-providers/{name}", json={"claim_mappings": []}, headers=auth_headers(admin_user))
    assert r.status_code == 200 and r.json()["claim_mappings"] == []
