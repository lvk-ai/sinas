"""Identity provider spec (docs/design/oidc-sso.md).

Config YAML (camelCase) and the REST API (snake_case) describe a provider
with the same shape; the aliases on SpecModel translate.
"""

import re
from typing import Any, Optional
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from app.schemas.spec.base import SpecModel

DEFAULT_SCOPES = ["openid", "email", "profile"]

_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def normalize_domain(value: str) -> str:
    """"@Example.com " -> "example.com"; anything that isn't a domain is refused."""
    domain = value.strip().lower().lstrip("@")
    if not _DOMAIN.match(domain):
        raise ValueError(f"'{value}' is not an email domain")
    return domain


def _domains(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        domain = normalize_domain(value)
        if domain not in seen:
            seen.append(domain)
    return seen


def check_issuer(value: str) -> str:
    """https, or http for a local IdP (or anywhere with OIDC_ALLOW_HTTP); no
    credentials, query or fragment (discovery appends to the path)."""
    from app.core.config import settings

    parts = urlsplit(value)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("issuer must be an http(s) URL")
    if parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS and not settings.oidc_allow_http:
        raise ValueError("issuer must use https (set OIDC_ALLOW_HTTP for a lab IdP)")
    if parts.username or parts.password:
        raise ValueError("issuer must not carry credentials")
    if parts.query or parts.fragment:
        raise ValueError("issuer must not have a query or fragment")
    return value


class RoleMappingSpec(SpecModel):
    """One rule: a matcher (exactly one of them) and the roles it grants."""

    group: Optional[str] = Field(default=None, min_length=1, max_length=512)  # a value in the groups claim
    email_domain: Optional[str] = None  # the user's email domain
    roles: list[str] = Field(min_length=1)

    @field_validator("email_domain")
    @classmethod
    def _domain(cls, value: Optional[str]) -> Optional[str]:
        return normalize_domain(value) if value is not None else None

    @field_validator("roles")
    @classmethod
    def _roles(cls, value: list[str]) -> list[str]:
        roles = [r.strip() for r in value]
        if any(not r for r in roles):
            raise ValueError("role names can't be empty")
        return list(dict.fromkeys(roles))

    @model_validator(mode="after")
    def _one_matcher(self) -> "RoleMappingSpec":
        if (self.group is None) == (self.email_domain is None):
            raise ValueError("a role mapping needs exactly one of group or emailDomain")
        return self

    def matches(self, groups: set[str], email_domain: Optional[str]) -> bool:
        if self.group is not None:
            return self.group in groups
        return email_domain is not None and email_domain == self.email_domain


class IdentityProviderSpec(SpecModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    display_name: str = Field(min_length=1, max_length=255)
    issuer: str = Field(min_length=1, max_length=2048)
    client_id: str = Field(min_length=1, max_length=512)
    # The name of a shared Secret holding the client secret, never the
    # value. None: a public client (PKCE only).
    client_secret: Optional[str] = Field(default=None, max_length=255)
    scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_SCOPES))
    email_claim: str = Field(default="email", min_length=1, max_length=512)
    groups_claim: str = Field(default="groups", min_length=1, max_length=512)
    role_mappings: list[RoleMappingSpec] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    auto_provision: bool = False
    default_role: Optional[str] = Field(default=None, max_length=255)
    enforce_for_domains: list[str] = Field(default_factory=list)
    session_hours: int = Field(default=12, ge=1, le=24 * 90)
    is_active: bool = True

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            # The console sends "" for none.
            for field in ("client_secret", "clientSecret", "default_role", "defaultRole"):
                if data.get(field) == "":
                    data[field] = None
        return data

    @field_validator("issuer")
    @classmethod
    def _issuer(cls, value: str) -> str:
        return check_issuer(value.strip())

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, value: list[str]) -> list[str]:
        scopes = list(dict.fromkeys(s.strip() for s in value if s.strip()))
        if "openid" not in scopes:
            raise ValueError("scopes must include openid")
        if any(" " in s for s in scopes):
            raise ValueError("one scope per entry")
        return scopes

    @field_validator("allowed_domains", "enforce_for_domains")
    @classmethod
    def _domain_list(cls, value: list[str]) -> list[str]:
        return _domains(value)

    @property
    def key(self) -> str:
        return self.name

    def role_names(self) -> set[str]:
        names = {role for rule in self.role_mappings for role in rule.roles}
        if self.default_role:
            names.add(self.default_role)
        return names

    def to_config(self) -> dict[str, Any]:
        out = super().to_config()
        if not out.get("roleMappings"):
            out.pop("roleMappings", None)
        for field in ("allowedDomains", "enforceForDomains"):
            if not out.get(field):
                out.pop(field, None)
        return out
