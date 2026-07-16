"""
User identity resolution and linking.

A user is addressable by three equivalent identifiers:
- internal UUID (users.id)
- email (users.email, unique)
- external identity (user_identities provider + subject, unique)
"""
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import normalize_email
from app.models.user import User, UserIdentity

logger = logging.getLogger(__name__)


class IdentityConflictError(Exception):
    """Raised when an identity is already linked to a different user."""

    def __init__(self, provider: str, subject: str):
        self.provider = provider
        self.subject = subject
        super().__init__(
            f"Identity '{provider}:{subject}' is already linked to another user"
        )


async def get_user_by_identity(
    db: AsyncSession, provider: str, subject: str
) -> Optional[User]:
    """Look up a user by external identity (provider, subject)."""
    result = await db.execute(
        select(User)
        .join(UserIdentity, UserIdentity.user_id == User.id)
        .where(UserIdentity.provider == provider, UserIdentity.subject == subject)
    )
    return result.scalar_one_or_none()


async def resolve_user(
    db: AsyncSession,
    *,
    user_id: Optional[str] = None,
    email: Optional[str] = None,
    provider: Optional[str] = None,
    subject: Optional[str] = None,
) -> Optional[User]:
    """
    Resolve a user by any supported identifier. Exactly one of user_id, email,
    or (provider, subject) must be given.
    """
    if user_id is not None:
        result = await db.execute(select(User).where(User.id == uuid.UUID(str(user_id))))
        return result.scalar_one_or_none()
    if email is not None:
        result = await db.execute(select(User).where(User.email == normalize_email(email)))
        return result.scalar_one_or_none()
    if provider is not None and subject is not None:
        return await get_user_by_identity(db, provider, subject)
    raise ValueError("resolve_user requires user_id, email, or provider+subject")


async def link_user_identity(
    db: AsyncSession,
    user: User,
    provider: str,
    subject: str,
    metadata: Optional[dict[str, Any]] = None,
) -> UserIdentity:
    """
    Link an external identity to a user (upsert). Refreshes metadata and
    last_synced_at if the identity is already linked to this user; raises
    IdentityConflictError if it is linked to a different user.
    """
    result = await db.execute(
        select(UserIdentity).where(
            UserIdentity.provider == provider, UserIdentity.subject == subject
        )
    )
    identity = result.scalar_one_or_none()

    if identity:
        if identity.user_id != user.id:
            raise IdentityConflictError(provider, subject)
        if metadata is not None:
            identity.identity_metadata = metadata
        identity.last_synced_at = datetime.now(UTC)
        return identity

    identity = UserIdentity(
        user_id=user.id,
        provider=provider,
        subject=subject,
        identity_metadata=metadata,
        last_synced_at=datetime.now(UTC),
    )
    db.add(identity)
    await db.flush()
    return identity
