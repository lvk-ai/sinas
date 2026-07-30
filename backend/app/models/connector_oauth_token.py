"""Per-user OAuth 2.0 tokens for connectors using the authorization-code grant.

One row per (connector, user). Access and refresh tokens are encrypted at rest with
the same Fernet-based encryption_service used for Secrets; expires_at is kept in a
plain column so the resolver can decide when to refresh without decrypting anything.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at, updated_at, uuid_pk


class ConnectorOAuthToken(Base):
    """A user's stored OAuth token for a connector (authorization-code grant)."""

    __tablename__ = "connector_oauth_tokens"

    id: Mapped[uuid_pk]
    connector_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connectors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Ciphertext produced by encryption_service.encrypt (never store raw tokens).
    encrypted_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_refresh_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Plain columns so refresh decisions don't require decryption.
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    scope: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_type: Mapped[str] = mapped_column(String(40), nullable=False, default="Bearer", server_default="Bearer")

    created_at: Mapped[created_at]
    updated_at: Mapped[updated_at]

    __table_args__ = (
        UniqueConstraint("connector_id", "user_id", name="uq_connector_oauth_token_connector_user"),
    )
