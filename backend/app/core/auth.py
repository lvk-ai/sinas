"""Authentication and authorization system with OTP, JWT, and API keys."""
import hashlib
import logging
import random
import secrets
import string
import uuid as uuid_lib
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

import bcrypt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.email import send_otp_email_async
from app.core.token_signing import decode_access_token, get_signing_context
from app.core.permissions import (
    DEFAULT_ROLE_DESCRIPTIONS,
    DEFAULT_ROLE_PERMISSIONS,
    SYSTEM,
    SYSTEM_ROLES,
    check_permission,
    validate_permission_subset,
)
from app.models import (
    APIKey,
    APIKeyRole,
    OTPSession,
    PasswordResetToken,
    RefreshToken,
    Role,
    RolePermission,
    User,
    UserRole,
)

security = HTTPBearer()


def normalize_email(email: str) -> str:
    """Normalize email address to lowercase."""
    return email.strip().lower()


def generate_otp_code(length: int = 6) -> str:
    """Generate a random numeric OTP code."""
    return "".join(random.choices(string.digits, k=length))


def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def hash_reset_token(plain_token: str) -> str:
    """At-rest form of a password reset/setup token (SHA-256: the token is
    256 random bits, so a slow hash adds nothing)."""
    return hashlib.sha256(plain_token.encode()).hexdigest()


# PasswordResetToken.purpose values.
RESET_LINK = "link"  # handed out by an admin / the setup-link CLI
SETUP_SESSION = "setup_session"  # what exchanging a link returns; never a link


# Every change to a user's password or reset tokens runs in one transaction
# holding this lock: minting, redeeming, exchanging a link for a setup
# session, completing setup, and every password change. Serialised per user,
# a password change can never miss a token minted concurrently with it, and
# two concurrent mints can't both leave a usable link.
async def lock_user_for_credentials(db: AsyncSession, user_id) -> None:
    """SELECT ... FOR UPDATE on the user row, held until the caller's
    transaction ends (commit or rollback)."""
    await db.execute(
        select(User.id).where(User.id == uuid_lib.UUID(str(user_id))).with_for_update()
    )


async def revoke_tokens_locked(db: AsyncSession, user_id) -> int:
    """Every unused token of the user, links and setup sessions alike. Caller
    holds the user lock and commits."""
    result = await db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == uuid_lib.UUID(str(user_id)),
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=datetime.now(UTC))
    )
    return result.rowcount or 0


async def issue_reset_token_locked(
    db: AsyncSession,
    user_id,
    purpose: str = RESET_LINK,
    created_by: Optional[str] = None,
    ttl: Optional[timedelta] = None,
) -> tuple[str, PasswordResetToken]:
    """Revoke the user's outstanding tokens and insert a new one, without
    committing. Caller holds the user lock."""
    now = datetime.now(UTC)
    plain_token = secrets.token_urlsafe(32)
    await revoke_tokens_locked(db, user_id)
    record = PasswordResetToken(
        user_id=uuid_lib.UUID(str(user_id)),
        token_hash=hash_reset_token(plain_token),
        expires_at=now + (ttl or timedelta(hours=settings.password_reset_token_ttl_hours)),
        created_by=uuid_lib.UUID(str(created_by)) if created_by else None,
        purpose=purpose,
    )
    db.add(record)
    await db.flush()
    return plain_token, record


async def create_password_reset_token(
    db: AsyncSession,
    user_id: str,
    created_by: Optional[str] = None,
    ttl: Optional[timedelta] = None,
) -> tuple[str, PasswordResetToken]:
    """
    Create a one-time password reset link. Plaintext is returned to the caller
    once; only the hash is stored.

    Newest link wins: every earlier unused token for the user (links and
    setup sessions) is revoked in the same transaction, under the user lock,
    so re-issuing a link kills the one sent before, even concurrently.
    """
    await lock_user_for_credentials(db, user_id)
    plain_token, record = await issue_reset_token_locked(
        db, user_id, RESET_LINK, created_by=created_by, ttl=ttl
    )
    await db.commit()
    await db.refresh(record)
    return plain_token, record


async def take_reset_token(
    db: AsyncSession, plain_token: str, purpose: str = RESET_LINK
) -> Optional[PasswordResetToken]:
    """
    Lock the token's user and consume the token if it is unused, unexpired and
    of `purpose`. Does NOT commit: the caller finishes its work (set a
    password, mint a setup session) in the same transaction, under the lock,
    then commits. None (and nothing locked) for an unusable token.

    The consume is re-checked under the lock: a token revoked by a password
    change that committed first no longer matches.
    """
    if not plain_token:
        return None
    token_hash = hash_reset_token(plain_token)
    user_id = (
        await db.execute(
            select(PasswordResetToken.user_id).where(PasswordResetToken.token_hash == token_hash)
        )
    ).scalar_one_or_none()
    if user_id is None:
        return None
    await lock_user_for_credentials(db, user_id)
    now = datetime.now(UTC)
    token_id = (
        await db.execute(
            update(PasswordResetToken)
            .where(
                PasswordResetToken.token_hash == token_hash,
                PasswordResetToken.purpose == purpose,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > now,
            )
            .values(used_at=now)
            .returning(PasswordResetToken.id)
        )
    ).scalar_one_or_none()
    if token_id is None:
        # Nothing changed; end the transaction to release the lock. (commit,
        # not rollback: a caller's session may be nested in an outer
        # transaction it must not roll back.)
        await db.commit()
        return None
    return await db.get(PasswordResetToken, token_id, populate_existing=True)


async def consume_password_reset_token(
    db: AsyncSession, plain_token: str, purpose: str = RESET_LINK
) -> Optional[PasswordResetToken]:
    """
    Validate and consume a password reset token of `purpose` (default: a
    link). Returns the record (used_at set) or None if the token is invalid,
    expired, already used, revoked, or of another purpose. Commits.
    """
    record = await take_reset_token(db, plain_token, purpose)
    if record is not None:
        await db.commit()
    return record


async def revoke_outstanding_password_reset_tokens(db: AsyncSession, user_id) -> int:
    """Mark every unused reset token (links and setup sessions) of a user as
    used, under the user lock. Commits."""
    await lock_user_for_credentials(db, user_id)
    count = await revoke_tokens_locked(db, user_id)
    await db.commit()
    return count


async def apply_new_password(db: AsyncSession, user: User, password_hash: str) -> None:
    """Set a user's password: under the user lock, with every outstanding
    reset link and setup session revoked in the same transaction. Commits.
    Hash the password (slow) before calling, not under the lock. Every path
    that changes a password goes through here (or holds the lock itself)."""
    await lock_user_for_credentials(db, user.id)
    user.password_hash = password_hash
    await revoke_tokens_locked(db, user.id)
    await db.commit()


async def warn_if_users_lack_passwords(db: AsyncSession) -> None:
    """
    When AUTH_MODE includes password, log a warning listing how many users have
    no password_hash set yet. Those users cannot log in until an admin generates
    a reset link for them. No-op when AUTH_MODE is OTP-only.
    """
    import logging

    logger = logging.getLogger(__name__)

    if "password" not in settings.auth_mode:
        return

    result = await db.execute(
        select(User).where(User.password_hash.is_(None))
    )
    users_without_password = result.scalars().all()
    count = len(users_without_password)
    if count == 0:
        return

    sample = ", ".join(u.email for u in users_without_password[:5])
    suffix = f" (showing first 5: {sample})" if count > 5 else f" ({sample})"
    logger.warning(
        f"AUTH_MODE={settings.auth_mode} but {count} user(s) have no password set "
        f"and cannot sign in until they get a setup link (an admin's reset link, or "
        f"`python -m app.cli setup-link --email <email>`){suffix}"
    )


async def revoke_all_refresh_tokens(db: AsyncSession, user_id: str) -> int:
    """Revoke every active refresh token for a user. Returns the count revoked."""
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == uuid_lib.UUID(user_id),
            RefreshToken.is_revoked == False,
        )
    )
    tokens = result.scalars().all()
    now = datetime.now(UTC)
    for tok in tokens:
        tok.is_revoked = True
        tok.revoked_at = now
    if tokens:
        await db.commit()
    return len(tokens)


async def create_otp_session(db: AsyncSession, email: str) -> OTPSession:
    """
    Create a new OTP session and send the code via email.

    Args:
        db: Database session
        email: User's email address

    Returns:
        Created OTPSession
    """
    otp_code = generate_otp_code()
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.otp_expire_minutes)

    # Delete any existing OTP sessions for this email
    result = await db.execute(select(OTPSession).where(OTPSession.email == normalize_email(email)))
    existing_sessions = result.scalars().all()
    for session in existing_sessions:
        await db.delete(session)

    # Create new OTP session
    otp_session = OTPSession(email=normalize_email(email), otp_code=otp_code, expires_at=expires_at)
    db.add(otp_session)
    await db.commit()
    await db.refresh(otp_session)

    # Send OTP email
    await send_otp_email_async(db, email, otp_code)

    return otp_session


async def verify_otp_code(db: AsyncSession, session_id: str, otp_code: str) -> Optional[OTPSession]:
    """
    Verify an OTP code against a session.

    Args:
        db: Database session
        session_id: OTP session ID
        otp_code: OTP code to verify

    Returns:
        OTPSession if valid, None otherwise
    """
    result = await db.execute(select(OTPSession).where(OTPSession.id == session_id))
    otp_session = result.scalar_one_or_none()

    if not otp_session:
        return None

    # Check if expired
    if otp_session.expires_at < datetime.now(UTC):
        return None

    # Check if already verified
    if otp_session.verified:
        return None

    # Check if max attempts exceeded
    if otp_session.attempts >= settings.otp_max_attempts:
        return None

    # Increment attempt counter
    otp_session.attempts += 1

    # Check if code matches
    if otp_session.otp_code != otp_code:
        await db.commit()
        return None

    # Mark as verified
    otp_session.verified = True
    await db.commit()

    return otp_session


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    """
    Get user by email address.

    Args:
        db: Database session
        email: User's email address

    Returns:
        User object or None if not found
    """
    normalized_email = normalize_email(email)

    result = await db.execute(select(User).where(User.email == normalized_email))
    return result.scalar_one_or_none()


async def get_user_permissions(db: AsyncSession, user_id: str) -> dict[str, bool]:
    """
    Get all permissions for a user by aggregating from their active roles.

    Args:
        db: Database session
        user_id: User's UUID

    Returns:
        Dictionary of permission_key: bool
    """
    # Get all active role memberships
    result = await db.execute(
        select(UserRole).where(UserRole.user_id == user_id, UserRole.active == True)
    )
    memberships = result.scalars().all()

    if not memberships:
        return {}

    # Collect all permissions from all roles
    all_permissions = {}

    for membership in memberships:
        result = await db.execute(
            select(RolePermission).where(RolePermission.role_id == membership.role_id)
        )
        role_permissions = result.scalars().all()

        for perm in role_permissions:
            # OR logic: if ANY role grants permission (true), user has it
            # Don't let a false permission override an existing true permission
            if perm.permission_value or perm.permission_key not in all_permissions:
                all_permissions[perm.permission_key] = perm.permission_value

    # Return permissions as-is (with wildcards) - they will be matched at runtime
    return all_permissions


def create_access_token(
    user_id: str,
    email: str,
    expires_delta: Optional[timedelta] = None,
    execution_depth: Optional[int] = None,
    context: Optional[dict[str, Any]] = None,
) -> str:
    """
    Create JWT access token (short-lived, no permissions in payload).

    Best Practice: Permissions are fetched from DB on each request,
    not embedded in token. This ensures immediate permission updates.

    Args:
        user_id: User UUID
        email: User email
        expires_delta: Optional custom expiration

    Returns:
        Encoded JWT token
    """
    now = datetime.now(UTC)

    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.access_token_expire_minutes)

    to_encode = {
        "sub": str(user_id),
        "email": email,
        "iat": int(now.timestamp()),  # Issued at (best practice)
        "exp": int(expire.timestamp()),
    }
    if execution_depth is not None:
        # Nesting depth of the execution this token belongs to. A nested call
        # (a function/agent invoking another execution) reads this to compute
        # its own depth and is rejected past settings.max_execution_depth.
        # Absent on real user/app tokens (treated as top-level callers).
        to_encode["execution_depth"] = execution_depth
    if context:
        # The session's attribution context (app/providers/attribution.py):
        # set by token exchange or an SSO session claim, kept by refresh.
        to_encode["context"] = context

    ctx = get_signing_context()
    headers = None
    if ctx.algorithm == "RS256":
        # iss/aud only under RS256: adding them to HS256 tokens would break
        # any existing consumer whose JWT library auto-verifies aud when the
        # claim is present. kid lets JWKS verifiers pick the right key.
        to_encode["iss"] = settings.token_issuer
        to_encode["aud"] = settings.jwt_audience
        headers = {"kid": ctx.kid}

    return jwt.encode(to_encode, ctx.sign_key, algorithm=ctx.algorithm, headers=headers)


def get_execution_depth_from_request(request) -> Optional[int]:
    """Read the `execution_depth` claim from the request's bearer token.

    Returns the *caller* execution's depth, or None when the caller is not an
    execution (a real user/app token carries no such claim). Best-effort: any
    decode failure returns None, i.e. the caller is treated as top-level.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header[7:].strip()
    try:
        payload = decode_access_token(token)
    except Exception:
        return None
    depth = payload.get("execution_depth")
    if depth is None:
        return None
    try:
        return int(depth)
    except (TypeError, ValueError):
        return None


async def create_refresh_token(
    db: AsyncSession,
    user_id: str,
    *,
    max_lifetime: Optional[timedelta] = None,
    identity_provider: Optional[str] = None,
    context: Optional[dict[str, Any]] = None,
) -> tuple[str, "RefreshToken"]:
    """
    Create a refresh token and store it in the database.

    Refresh tokens are long-lived and stored in DB for revocation control.

    Args:
        db: Database session
        user_id: User UUID
        max_lifetime: Cap below refresh_token_expire_days (an SSO login's
            session_hours: the user goes back through the IdP after it)
        identity_provider: The provider whose SSO login this is

    Returns:
        Tuple of (plain_token, refresh_token_model)
    """


    # Generate random token
    plain_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(plain_token.encode()).hexdigest()
    token_prefix = plain_token[:8]

    # Calculate expiration
    lifetime = timedelta(days=settings.refresh_token_expire_days)
    if max_lifetime is not None:
        lifetime = min(lifetime, max_lifetime)
    expires_at = datetime.now(UTC) + lifetime

    # Create refresh token record
    refresh_token = RefreshToken(
        user_id=uuid_lib.UUID(user_id),
        token_hash=token_hash,
        token_prefix=token_prefix,
        is_revoked=False,
        expires_at=expires_at,
        identity_provider=identity_provider,
        # Every access token it mints carries it; it never changes.
        context=context or None,
    )

    db.add(refresh_token)
    await db.commit()
    await db.refresh(refresh_token)

    return plain_token, refresh_token


async def validate_refresh_token(db: AsyncSession, plain_token: str) -> Optional[tuple[str, str]]:
    """
    Validate a refresh token and return user info.

    Args:
        db: Database session
        plain_token: Plain refresh token from request

    Returns:
        Tuple of (user_id, email) if valid, None otherwise
    """

    token_hash = hashlib.sha256(plain_token.encode()).hexdigest()

    # Find active refresh token
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == token_hash, RefreshToken.is_revoked == False
        )
    )
    refresh_token = result.scalar_one_or_none()

    if not refresh_token:
        return None

    # Check if expired
    if refresh_token.expires_at < datetime.now(UTC):
        return None

    result = await db.execute(select(User).where(User.id == refresh_token.user_id))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        return None

    await _refresh_usage_stamps(
        db, datetime.now(UTC), refresh_token=refresh_token, user=user
    )

    return str(user.id), user.email


async def revoke_refresh_token(db: AsyncSession, plain_token: str) -> bool:
    """
    Revoke a refresh token (logout).

    Args:
        db: Database session
        plain_token: Plain refresh token to revoke

    Returns:
        True if revoked, False if not found
    """

    token_hash = hashlib.sha256(plain_token.encode()).hexdigest()

    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    refresh_token = result.scalar_one_or_none()

    if not refresh_token:
        return False

    refresh_token.is_revoked = True
    refresh_token.revoked_at = datetime.now(UTC)
    await db.commit()

    return True


# API Key Management


def generate_api_key() -> tuple[str, str, str]:
    """
    Generate a new API key.

    Returns:
        Tuple of (full_key, key_hash, key_prefix)
    """
    key = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    key_prefix = key[:8]
    return key, key_hash, key_prefix


def hash_api_key(key: str) -> str:
    """Hash an API key for comparison."""
    return hashlib.sha256(key.encode()).hexdigest()


async def create_api_key(
    db: AsyncSession,
    user: User,
    name: str,
    permissions: dict[str, bool],
    expires_at: Optional[datetime] = None,
    created_by: Optional[User] = None,
) -> tuple[APIKey, str]:
    """
    Create a new API key for a user.

    Args:
        db: Database session
        user: User to create key for
        name: Friendly name for the key
        permissions: Permission dict (must be subset of user's permissions)
        expires_at: Optional expiration date
        created_by: User creating the key

    Returns:
        Tuple of (api_key_model, plain_key)

    Raises:
        HTTPException if permissions are not a subset of user's permissions
    """
    # Get user's maximum permissions from roles
    user_permissions = await get_user_permissions(db, str(user.id))

    # Validate that requested permissions are a subset of user permissions
    is_valid, violations = validate_permission_subset(permissions, user_permissions)

    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"API key permissions exceed user's role permissions. Violations: {', '.join(violations)}",
        )

    # Generate key
    plain_key, key_hash, key_prefix = generate_api_key()

    # Create API key record
    api_key = APIKey(
        user_id=user.id,
        name=name,
        key_hash=key_hash,
        key_prefix=key_prefix,
        permissions=permissions,
        is_active=True,
        expires_at=expires_at,
        created_by=created_by.id if created_by else user.id,
    )

    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    return api_key, plain_key


async def resolve_api_key_permissions(
    db: AsyncSession, api_key: APIKey, user: User
) -> dict[str, bool]:
    """
    Effective permissions for an API key at request time.

    Union of the linked roles' permissions, overlaid with the key's explicit
    grants, then capped by the owner's LIVE role permissions: a granted entry
    survives only while the owner currently holds it. Keys therefore track
    role edits and owner demotions instead of freezing a mint-time snapshot —
    revoking a permission from a role or user revokes it from their keys too.
    """
    perms: dict[str, bool] = {}

    # Union of linked roles' permissions (same OR logic as user roles)
    result = await db.execute(
        select(RolePermission)
        .join(APIKeyRole, APIKeyRole.role_id == RolePermission.role_id)
        .where(APIKeyRole.api_key_id == api_key.id)
    )
    for perm in result.scalars().all():
        if perm.permission_value or perm.permission_key not in perms:
            perms[perm.permission_key] = perm.permission_value

    # Explicit grants override role-derived entries (an explicit False is a
    # deliberate denial on this key)
    perms.update(api_key.permissions or {})

    if not any(perms.values()):
        return perms

    owner_perms = await get_user_permissions(db, str(user.id))
    return {k: v for k, v in perms.items() if not v or check_permission(owner_perms, k)}


# How stale a usage stamp may get before it is rewritten.
#
# last_used_at / last_login_at are observability, not authorisation, and
# nothing reads them at sub-minute resolution. Stamping them on EVERY
# authenticated request made the auth path serialise: a service calling Sinas
# uses one API key, so every concurrent request needed a row lock on that one
# api_keys row — and on the one users row behind it — so effective concurrency
# on authentication fell to one. Under sustained load that queue becomes
# self-sustaining: observed on a local stack as 33 concurrent
# `UPDATE api_keys SET last_used_at` all waiting on transactionid, the oldest
# for 643 seconds, while uploads timed out and even /health took six seconds.
USAGE_STAMP_MAX_AGE = timedelta(minutes=1)


def _stamp_is_stale(stamp: Optional[datetime], now: datetime) -> bool:
    if stamp is None:
        return True
    if stamp.tzinfo is None:  # legacy rows written before tz-aware stamps
        stamp = stamp.replace(tzinfo=UTC)
    return (now - stamp) >= USAGE_STAMP_MAX_AGE


async def _refresh_usage_stamps(
    db: AsyncSession,
    now: datetime,
    *,
    api_key: Optional[APIKey] = None,
    refresh_token: Optional[RefreshToken] = None,
    user: Optional[User] = None,
) -> None:
    """Rewrite usage stamps that have gone stale, and nothing else.

    Two things keep this off the hot path. The in-memory check skips the
    statement entirely for the ~all requests that arrive inside the window.
    When one does fire it is a CONDITIONAL update, so a concurrent caller that
    blocks on the row re-evaluates the WHERE after the winner commits, finds
    the stamp already fresh, matches no rows and takes no lock of its own —
    a thundering herd at window expiry resolves after a single write instead
    of queueing one per request.

    Core UPDATEs, deliberately: assigning to the ORM objects would leave them
    dirty and flush again later in the request, reopening the contention this
    exists to remove.
    """
    cutoff = now - USAGE_STAMP_MAX_AGE
    wrote = False

    if api_key is not None and _stamp_is_stale(api_key.last_used_at, now):
        await db.execute(
            update(APIKey)
            .where(
                APIKey.id == api_key.id,
                or_(APIKey.last_used_at.is_(None), APIKey.last_used_at < cutoff),
            )
            .values(last_used_at=now)
        )
        wrote = True

    if refresh_token is not None and _stamp_is_stale(refresh_token.last_used_at, now):
        await db.execute(
            update(RefreshToken)
            .where(
                RefreshToken.id == refresh_token.id,
                or_(
                    RefreshToken.last_used_at.is_(None),
                    RefreshToken.last_used_at < cutoff,
                ),
            )
            .values(last_used_at=now)
        )
        wrote = True

    if user is not None and _stamp_is_stale(user.last_login_at, now):
        await db.execute(
            update(User)
            .where(
                User.id == user.id,
                or_(User.last_login_at.is_(None), User.last_login_at < cutoff),
            )
            .values(last_login_at=now)
        )
        wrote = True

    if wrote:
        await db.commit()


# The API key the current request or agent run acts through, if any. Agent
# tools check the caller's permissions; for a key, that's the key's — narrower
# than its owner's. Set where a key authenticates, carried into queued agent
# jobs (queue_service), read by get_effective_permissions.
_acting_api_key: ContextVar[Optional[str]] = ContextVar("sinas_acting_api_key", default=None)


def current_api_key_id() -> Optional[str]:
    return _acting_api_key.get()


def bind_api_key(api_key_id: Optional[str]):
    """Act through this API key (None: none) for the rest of this task.
    Returns a token for reset_api_key."""
    return _acting_api_key.set(str(api_key_id) if api_key_id else None)


def reset_api_key(token) -> None:
    _acting_api_key.reset(token)


# The attribution context of the credential the current request came in on:
# an access token's `context` claim, or an API key's stored context. Bound
# where the credential authenticates (verify_jwt_or_api_key); read when a
# chat takes its attribution snapshot (app/providers/attribution.py).
_credential_context: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "sinas_credential_context", default=None
)


def current_credential_context() -> Optional[dict[str, Any]]:
    return _credential_context.get()


def bind_credential_context(context: Optional[dict[str, Any]]):
    """Attribute this task's new chats by this context (None: none).
    Returns a token for reset_credential_context."""
    from app.providers.attribution import context_or_none

    return _credential_context.set(context_or_none(context))


def reset_credential_context(token) -> None:
    _credential_context.reset(token)


async def get_effective_permissions(db: AsyncSession, user_id: str) -> dict[str, bool]:
    """The permissions to check a call against: the user's, or — when the run
    acts through an API key — the key's live ones (revoked, expired or
    narrowed keys take effect at once; another user's key grants nothing)."""
    api_key_id = current_api_key_id()
    if not api_key_id:
        return await get_user_permissions(db, user_id)
    api_key = await db.get(APIKey, uuid_lib.UUID(str(api_key_id)))
    if (
        api_key is None
        or not api_key.is_active
        or (api_key.expires_at and api_key.expires_at < datetime.now(UTC))
        or str(api_key.user_id) != str(user_id)
    ):
        return {}
    user = await db.get(User, api_key.user_id)
    if user is None or not user.is_active:
        return {}
    return await resolve_api_key_permissions(db, api_key, user)


async def _lookup_api_key(db: AsyncSession, key: str) -> Optional[tuple[APIKey, User]]:
    """The active, unexpired key and its active owner, or None."""
    result = await db.execute(
        select(APIKey).where(APIKey.key_hash == hash_api_key(key), APIKey.is_active == True)
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        return None
    if api_key.expires_at and api_key.expires_at < datetime.now(UTC):
        return None
    result = await db.execute(select(User).where(User.id == api_key.user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        return None
    return api_key, user


async def validate_api_key(db: AsyncSession, key: str) -> Optional[tuple[User, dict[str, bool]]]:
    """
    Validate an API key and return the user and permissions.

    Args:
        db: Database session
        key: Plain API key from request

    Returns:
        Tuple of (user, permissions) if valid, None otherwise
    """
    found = await _lookup_api_key(db, key)
    if not found:
        return None
    api_key, user = found
    await _refresh_usage_stamps(db, datetime.now(UTC), api_key=api_key, user=user)
    # Everything this request does — agent runs and their tools included —
    # acts through the key, and new chats are attributed by its context.
    bind_api_key(str(api_key.id))
    bind_credential_context(api_key.context)
    return user, await resolve_api_key_permissions(db, api_key, user)


# Authentication Dependencies

# HTTPBearer security scheme for Swagger UI
http_bearer = HTTPBearer(auto_error=False)


async def _verify_component_token(
    claims: dict, request: Optional[Request], db: AsyncSession
) -> tuple[str, str, dict[str, bool]]:
    """A component access token (services/component_access): the viewer,
    with permissions capped to the component's grants, on the component's
    routes only."""
    from app.models.component import Component
    from app.models.component_share import ComponentShare
    from app.services.component_access import (
        ComponentScope,
        route_allowed,
        scoped_permissions,
        share_is_live,
    )

    scope = ComponentScope(claims["namespace"], claims["name"], int(claims["session_start"]))
    route = request.scope.get("route") if request is not None else None
    if not route_allowed(
        getattr(route, "path", None), dict(request.path_params) if request else {}, scope
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A component token can't be used here",
        )

    user = (await db.execute(select(User).where(User.id == claims["sub"]))).scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    component = await Component.get_by_name(db, scope.namespace, scope.name)
    if not component or not component.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Component not found")

    read_only = False
    if claims.get("share_id"):
        # A creator link: it acts for its creator only while the link lives
        # (revoking or expiring it ends access at once), for this component.
        share = await db.get(ComponentShare, claims["share_id"])
        if (
            not share_is_live(share)
            or share.mode != "creator"
            or share.component_id != component.id
            or str(share.created_by) != str(user.id)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Share link no longer valid"
            )
        read_only = not share.allow_writes
        scope = ComponentScope(
            scope.namespace, scope.name, scope.session_start,
            share_id=str(share.id), read_only=read_only,
        )

    # The render's attribution context; for a page rendered for an API key,
    # the key's own (stored, immutable) context.
    from app.providers.attribution import context_or_none

    context = context_or_none(claims.get("context"))
    if claims.get("api_key_id"):
        # Rendered for an API key: the page acts through that key, capped by
        # its (live) permissions rather than its owner's.
        bind_api_key(claims["api_key_id"])
        try:
            key = await db.get(APIKey, uuid_lib.UUID(str(claims["api_key_id"])))
        except ValueError:
            key = None
        context = context_or_none(key.context) if key is not None else None
        scope = ComponentScope(
            scope.namespace, scope.name, scope.session_start,
            share_id=scope.share_id, read_only=scope.read_only,
            api_key_id=str(claims["api_key_id"]),
        )
    bind_credential_context(context)
    scope = ComponentScope(
        scope.namespace, scope.name, scope.session_start,
        share_id=scope.share_id, read_only=scope.read_only,
        api_key_id=scope.api_key_id, context=context,
    )

    permissions = scoped_permissions(
        component, await get_effective_permissions(db, str(user.id)), read_only
    )
    request.state.component_scope = scope
    return str(user.id), user.email, permissions


async def verify_jwt_or_api_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(http_bearer),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
) -> tuple[str, str, dict[str, bool]]:
    """
    Verify either JWT access token or API key from Authorization or X-API-Key header.

    BEST PRACTICE: Permissions are loaded from DB, not from JWT payload.
    This ensures immediate permission updates without waiting for token expiry.

    Supports:
    - Bearer <jwt_access_token> (15 min) in Authorization header
    - Bearer <api_key> (long-lived) in Authorization header
    - <api_key> in X-API-Key header

    Returns:
        Tuple of (user_id, email, permissions)

    Raises:
        HTTPException 401 if authentication fails
    """
    # Try X-API-Key header first
    if x_api_key:
        token = x_api_key
    elif credentials:
        token = credentials.credentials  # HTTPBearer already strips "Bearer " prefix
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authorization header"
        )

    from app.services.component_access import component_token_claims

    component_claims = component_token_claims(token)
    if component_claims is not None:
        return await _verify_component_token(component_claims, request, db)

    # Try JWT first
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        email = payload.get("email")

        # Purpose tokens (render, file-serve, component access) are not
        # access tokens, whatever else they carry.
        if not user_id or not email or payload.get("purpose"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload"
            )

        # BEST PRACTICE: Load permissions from database, not from JWT
        # This ensures permissions are always current (no stale token permissions)
        permissions = await get_user_permissions(db, str(user_id))

        # Verify user exists and is active (soft-deleted users lose access immediately)
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()

        if not user or not user.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

        # The session's attribution context, signed into the token (none on
        # tokens from before it existed, or that never had one).
        bind_credential_context(payload.get("context"))
        return str(user_id), email, permissions

    except JWTError:
        # If JWT fails, try API key
        result = await validate_api_key(db, token)

        if not result:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired credentials"
            )

        user, permissions = result
        if request is not None:
            # A key's permissions are narrower than its owner's; endpoints
            # that hand out the owner's (creator share links) refuse keys.
            request.state.via_api_key = True
        return str(user.id), user.email, permissions


def require_permission(required_permission: str):
    """
    Dependency factory to require a specific permission.

    Usage:
        @router.post("/chats")
        async def create_chat(
            user_id: str = Depends(require_permission("sinas.chats.create:own"))
        ):
            ...

    Args:
        required_permission: Permission string like "sinas.chats.create:own"

    Returns:
        Dependency function that returns user_id if authorized
    """

    async def permission_checker(
        request: Request,
        auth_data: tuple[str, str, dict[str, bool]] = Depends(verify_jwt_or_api_key),
    ) -> str:
        user_id, email, permissions = auth_data
        has_perm = check_permission(permissions, required_permission)

        # Store permission info in request state for logging
        request.state.user_id = user_id
        request.state.user_email = email
        request.state.permission_used = required_permission
        request.state.has_permission = has_perm

        if not has_perm:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {required_permission}",
            )

        return user_id

    return permission_checker


async def get_current_user(
    request: Request, auth_data: tuple[str, str, dict[str, bool]] = Depends(verify_jwt_or_api_key)
) -> str:
    """
    Get current authenticated user ID without requiring specific permission.

    Returns:
        user_id
    """
    user_id, email, _ = auth_data

    # Store user info in request state for logging
    request.state.user_id = user_id
    request.state.user_email = email

    return user_id


async def get_current_user_with_permissions(
    request: Request, auth_data: tuple[str, str, dict[str, bool]] = Depends(verify_jwt_or_api_key)
) -> tuple[str, dict[str, bool]]:
    """
    Get current authenticated user ID and their permissions.

    IMPORTANT: Endpoints using this dependency MUST call
    set_permission_used(request, "permission.key") to log
    the permission for compliance tracking.

    Returns:
        Tuple of (user_id, permissions)
    """
    user_id, email, permissions = auth_data

    # Store user info in request state for logging
    request.state.user_id = user_id
    request.state.user_email = email

    return user_id, permissions




def via_api_key(request: Optional[Request]) -> bool:
    """Whether this request authenticated with an API key. A key's
    permissions are narrower than its owner's, so endpoints that would hand
    out a credential acting with the owner's (component render tokens, share
    links that act as their creator) refuse or withhold it for keys."""
    return bool(request is not None and getattr(request.state, "via_api_key", False))


def set_permission_used(request: Request, permission: str, has_perm: bool = True):
    """
    Store permission decision in request state for compliance logging.

    Call this after manual permission checks in endpoint code.

    Example:
        if permissions.get("sinas.functions.read:all"):
            set_permission_used(request, "sinas.functions.read:all")
        elif permissions.get("sinas.functions.read:own"):
            set_permission_used(request, "sinas.functions.read:own")

    Args:
        request: FastAPI Request object
        permission: Permission key that was checked (e.g. "sinas.functions.read:all")
        has_perm: Whether user has the permission (default True)
    """
    request.state.permission_used = permission
    request.state.has_permission = has_perm


# Group initialization helper


async def initialize_default_roles(db: AsyncSession):
    """
    Initialize default roles (GuestUsers, Users, Deployers, Admins) with permissions.

    Should be called during application startup or setup.
    """
    for role_name, permissions in DEFAULT_ROLE_PERMISSIONS.items():
        # Check if role exists
        result = await db.execute(select(Role).where(Role.name == role_name))
        role = result.scalar_one_or_none()
        system_role = role_name in SYSTEM_ROLES

        if role is not None and system_role and role.managed_by != SYSTEM:
            # An instance's own role by this name: never seed permissions into
            # it (its members would gain them). The built-in one isn't added.
            logging.getLogger(__name__).warning(
                "A role named %r already exists and isn't the built-in one; leaving it as is "
                "(rename it to get the built-in %s role)", role_name, role_name,
            )
            continue

        if not role:
            # Create role
            role = Role(
                name=role_name,
                description=DEFAULT_ROLE_DESCRIPTIONS.get(role_name, f"Default {role_name} role"),
                managed_by=SYSTEM if system_role else None,
            )
            db.add(role)
            await db.commit()
            await db.refresh(role)

        # Sync default permissions:
        # - Only ADD permissions that don't exist yet (preserves admin customizations)
        # - Always force sinas.*:all = True on Admins (safety net)
        for perm_key, perm_value in permissions.items():
            result = await db.execute(
                select(RolePermission).where(
                    RolePermission.role_id == role.id, RolePermission.permission_key == perm_key
                )
            )
            existing_perm = result.scalar_one_or_none()

            if existing_perm:
                # Only force-overwrite the superadmin wildcard on Admins
                if role_name == "Admins" and perm_key == "sinas.*:all":
                    existing_perm.permission_value = True
            else:
                new_perm = RolePermission(
                    role_id=role.id, permission_key=perm_key, permission_value=perm_value
                )
                db.add(new_perm)

        await db.commit()


async def initialize_superadmin(db: AsyncSession):
    """
    Initialize superadmin user if SUPERADMIN_EMAIL is set.

    - Creates the user and grants Admins membership only when no other admins exist
      (prevents accidental auto-creation after manual setup).
    - When the user already exists, ensures Admins membership.

    Password bootstrap, when auth_mode includes "password" — two modes:

    - SUPERADMIN_PASSWORD set: the env var is authoritative. password_hash is synced
      to it on every boot (written only on mismatch), so a password changed in the
      UI reverts on restart while the var is set. This doubles as the "admin lost
      their password" escape hatch: change SUPERADMIN_PASSWORD and restart.
    - SUPERADMIN_PASSWORD unset: the superadmin owns their password. Nothing is
      touched once one is set. Until then, every boot logs one line telling the
      operator how to mint a one-time setup link (`python -m app.cli
      setup-link`, run inside the backend container). The link itself is never
      logged: minting takes a shell in the container, a stronger boundary than
      read access to logs, and it beats an unauthenticated "set password"
      endpoint that the first scanner to find the host could use.
    """
    import logging

    logger = logging.getLogger(__name__)

    if not settings.superadmin_email:
        return

    email = normalize_email(settings.superadmin_email)
    auth_mode_includes_password = "password" in settings.auth_mode

    result = await db.execute(select(Role).where(Role.name == "Admins"))
    admins_role = result.scalar_one_or_none()

    if not admins_role:
        logger.error("Admins role not found. Run initialize_default_roles first.")
        return

    user = await get_user_by_email(db, email)

    if not user:
        # Only auto-create if no other admins exist
        result = await db.execute(
            select(UserRole).where(UserRole.role_id == admins_role.id, UserRole.active == True)
        )
        if result.scalars().first():
            return

        user = User(email=email)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        logger.info(f"Admin user created: {email}")

    if not user.is_active:
        # Escape hatch: a soft-deleted superadmin is reactivated on restart
        user.is_active = True
        await db.commit()
        logger.info(f"Superadmin user reactivated: {email}")

    result = await db.execute(
        select(UserRole).where(UserRole.role_id == admins_role.id, UserRole.user_id == user.id)
    )
    existing_membership = result.scalar_one_or_none()

    if not existing_membership:
        membership = UserRole(role_id=admins_role.id, user_id=user.id, active=True)
        db.add(membership)
        await db.commit()

    if not auth_mode_includes_password:
        return

    if settings.superadmin_password:
        needs_update = not user.password_hash or not verify_password(
            settings.superadmin_password, user.password_hash
        )
        if needs_update:
            reverted = bool(user.password_hash)
            # Under the user lock, outstanding links/sessions revoked with it.
            await apply_new_password(db, user, hash_password(settings.superadmin_password))
            if reverted:
                logger.warning(
                    f"Superadmin password for {email} differed from SUPERADMIN_PASSWORD "
                    "and was reset to it. The env var is authoritative while set; "
                    "unset it to let the superadmin manage their own password."
                )
            else:
                logger.info(
                    f"Superadmin password set from SUPERADMIN_PASSWORD env var for {email}"
                )
        return

    if user.password_hash:
        return  # self-managed; never touch an existing password

    from app.services.setup_link import log_setup_hint

    # The how, never a token: anyone who can read logs must not be able to
    # claim the instance.
    await log_setup_hint(email, logger)
