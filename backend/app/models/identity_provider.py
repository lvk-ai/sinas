"""Identity providers: OpenID Connect IdPs people sign in to the console
with (docs/design/oidc-sso.md).

The client secret is never stored here: `client_secret` is the name of a
shared Secret, resolved when the code is exchanged.
"""
import uuid
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at, updated_at, uuid_pk


class IdentityProvider(Base):
    __tablename__ = "identity_providers"

    id: Mapped[uuid_pk]
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    issuer: Mapped[str] = mapped_column(String(2048), nullable=False)
    client_id: Mapped[str] = mapped_column(String(512), nullable=False)
    client_secret: Mapped[Optional[str]] = mapped_column(String(255))  # a shared Secret's name
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    email_claim: Mapped[str] = mapped_column(String(512), nullable=False, default="email")
    groups_claim: Mapped[str] = mapped_column(String(512), nullable=False, default="groups")
    # [{"group": ..., "roles": [...]}, {"email_domain": ..., "roles": [...]}]
    role_mappings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    allowed_domains: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    enforce_for_domains: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    auto_provision: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    default_role: Mapped[Optional[str]] = mapped_column(String(255))
    session_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=12, server_default="12")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))

    managed_by: Mapped[Optional[str]] = mapped_column(Text)
    config_name: Mapped[Optional[str]] = mapped_column(Text)
    config_checksum: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[created_at]
    updated_at: Mapped[updated_at]
