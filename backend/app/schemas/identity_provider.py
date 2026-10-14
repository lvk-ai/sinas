"""REST shapes for identity providers and the SSO login flow."""
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.auth import AuthUserResponse


class RoleMapping(BaseModel):
    group: Optional[str] = None  # a value in the groups claim
    email_domain: Optional[str] = None  # or: the user's email domain
    roles: list[str]


class IdentityProviderCreate(BaseModel):
    name: str
    display_name: str
    issuer: str
    client_id: str
    client_secret: Optional[str] = None  # the name of a shared Secret
    scopes: Optional[list[str]] = None
    email_claim: Optional[str] = None
    groups_claim: Optional[str] = None
    role_mappings: list[RoleMapping] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    auto_provision: bool = False
    default_role: Optional[str] = None
    enforce_for_domains: list[str] = Field(default_factory=list)
    session_hours: Optional[int] = None
    is_active: bool = True


class IdentityProviderUpdate(BaseModel):
    """Fields left out are unchanged; lists are replaced whole. The name
    can't change: it is in the redirect URI registered at the IdP. An empty
    client_secret or default_role clears it."""

    model_config = {"extra": "forbid"}

    display_name: Optional[str] = None
    issuer: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    scopes: Optional[list[str]] = None
    email_claim: Optional[str] = None
    groups_claim: Optional[str] = None
    role_mappings: Optional[list[RoleMapping]] = None
    allowed_domains: Optional[list[str]] = None
    auto_provision: Optional[bool] = None
    default_role: Optional[str] = None
    enforce_for_domains: Optional[list[str]] = None
    session_hours: Optional[int] = None
    is_active: Optional[bool] = None


class IdentityProviderResponse(BaseModel):
    id: uuid.UUID
    name: str
    display_name: str
    issuer: str
    client_id: str
    client_secret: Optional[str]
    scopes: list[str]
    email_claim: str
    groups_claim: str
    role_mappings: list[RoleMapping]
    allowed_domains: list[str]
    auto_provision: bool
    default_role: Optional[str]
    enforce_for_domains: list[str]
    session_hours: int
    is_active: bool
    managed_by: Optional[str]
    # What to register at the IdP as the redirect (callback) URI.
    redirect_uri: str
    created_at: datetime
    updated_at: Optional[datetime]

    @classmethod
    def of(cls, row) -> "IdentityProviderResponse":
        from app.services.sso import redirect_uri

        return cls(
            id=row.id, name=row.name, display_name=row.display_name, issuer=row.issuer,
            client_id=row.client_id, client_secret=row.client_secret, scopes=list(row.scopes or []),
            email_claim=row.email_claim, groups_claim=row.groups_claim,
            role_mappings=[RoleMapping(**rule) for rule in row.role_mappings or []],
            allowed_domains=list(row.allowed_domains or []), auto_provision=bool(row.auto_provision),
            default_role=row.default_role, enforce_for_domains=list(row.enforce_for_domains or []),
            session_hours=row.session_hours, is_active=row.is_active is not False,
            managed_by=row.managed_by, redirect_uri=redirect_uri(row.name),
            created_at=row.created_at, updated_at=row.updated_at,
        )


# ---------------------------------------------------------------- login flow


class SsoProviderInfo(BaseModel):
    """What the login page needs: no issuer, no client, no domains."""

    name: str
    display_name: str


class SsoDiscoverRequest(BaseModel):
    email: str = Field(max_length=320)


class SsoDiscoverResponse(BaseModel):
    # The provider this email's domain must sign in with, if any.
    provider: Optional[SsoProviderInfo] = None


class SsoExchangeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=256)
    code_verifier: str = Field(min_length=43, max_length=128)


class SsoExchangeResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: AuthUserResponse
    # The console page to continue on (a path inside the console).
    return_to: str = "/"
