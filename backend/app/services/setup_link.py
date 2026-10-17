"""Password setup links: `<console>/ui/?token=...`.

One mechanism with the admin "reset link": both mint a single-use
PasswordResetToken (hashed at rest, PASSWORD_RESET_TOKEN_TTL_HOURS, newest
link per user wins). What this module adds is the console flow around it:

1. The console trades the link token for a short-lived setup session
   (POST /auth/setup-link/exchange) the moment the page loads. That burns the
   link: a copy left in browser history, a proxy log or a chat preview is
   worthless once the person it was meant for has opened it.
2. The person chooses a password; POST /auth/setup-link/complete redeems the
   setup session, sets it, and signs them in the way AUTH_MODE signs anyone
   in after a correct password (tokens for "password", an emailed code for
   "password+otp").

A link never signs anyone in without setting a password, and it is not used
at all in OTP-only mode (see docs-mint/platform/authentication.mdx).
"""
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    RESET_LINK,
    SETUP_SESSION,
    create_password_reset_token,
    get_user_by_email,
    issue_reset_token_locked,
    revoke_tokens_locked,
    take_reset_token,
)
from app.core.config import settings
from app.models import PasswordResetToken, User

logger = logging.getLogger(__name__)

# What an operator runs inside the backend container to get a link.
SETUP_LINK_COMMAND = "python -m app.cli setup-link"

# How long the person has between opening the link and saving a password.
SETUP_SESSION_TTL = timedelta(minutes=30)

# The one message for every unusable link: never says which (or whose) it was.
INVALID_LINK_MESSAGE = "This link is invalid or has expired. Ask your administrator for a new one."


class SetupLinkError(Exception):
    """A setup link can't be minted (for the CLI to print)."""


def password_auth_enabled() -> bool:
    return "password" in settings.auth_mode


def setup_link_url(plain_token: str) -> str:
    """The console URL that carries a setup token. `/ui/` with the slash: the
    console's own path, so no redirect hop repeats the token in a Location
    header (`/ui?token=` works too)."""
    return f"{settings.public_console_url}/?token={plain_token}"


async def mint_setup_link(
    db: AsyncSession, email: str | None = None, created_by: str | None = None
) -> tuple[str, PasswordResetToken, User]:
    """Mint a setup link for `email` (default: SUPERADMIN_EMAIL). Returns the
    full URL, the token record and the user. Revokes any earlier link."""
    if not password_auth_enabled():
        raise SetupLinkError(
            f"AUTH_MODE={settings.auth_mode}: people sign in with a code emailed to them, "
            "so there is no password to set and no setup link to mint."
        )
    email = email or settings.superadmin_email
    if not email:
        raise SetupLinkError("No --email given and SUPERADMIN_EMAIL is not set.")
    user = await get_user_by_email(db, email)
    if user is None or not user.is_active:
        raise SetupLinkError(f"No active user with email {email}.")

    plain_token, record = await create_password_reset_token(
        db, str(user.id), created_by=created_by
    )
    logger.warning("Setup link minted for user %s", user.id)
    return setup_link_url(plain_token), record, user


async def _active_user(db: AsyncSession, user_id) -> User | None:
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    return user if user is not None and user.is_active else None


async def exchange_setup_link(
    db: AsyncSession, plain_token: str
) -> tuple[str, PasswordResetToken, User] | None:
    """Spend a link and hand back a setup session token (never put in a URL)
    for the same user. One transaction under the user lock: the link is
    consumed and the session created together, so a password change can't
    slip in between and miss the session. Only a *link* is accepted here: a
    setup session can never mint another one. None for any unusable link or
    account."""
    record = await take_reset_token(db, plain_token, RESET_LINK)
    if record is None:
        return None
    user = await _active_user(db, record.user_id)
    if user is None:
        await db.commit()  # the link stays spent
        return None
    ttl = min(SETUP_SESSION_TTL, timedelta(hours=settings.password_reset_token_ttl_hours))
    session_token, session = await issue_reset_token_locked(db, user.id, SETUP_SESSION, ttl=ttl)
    await db.commit()
    await db.refresh(session)
    logger.warning("Setup link opened for user %s", user.id)
    return session_token, session, user


async def redeem_setup_session(db: AsyncSession, session_token: str) -> User | None:
    """Consume a setup session (only a session: never a link) and return its
    user, leaving the transaction open with the user lock held so the caller
    sets the password in it (finish_setup). The session's validity is
    checked under the lock, so one revoked by a password change that
    committed first is refused. None (transaction ended) for any unusable
    session or account."""
    record = await take_reset_token(db, session_token, SETUP_SESSION)
    if record is None:
        return None
    user = await _active_user(db, record.user_id)
    if user is None:
        await db.commit()
        return None
    return user


async def finish_setup(db: AsyncSession, user: User, password_hash: str) -> None:
    """Set the password redeem_setup_session's caller chose, revoke every
    other outstanding link/session, commit (releasing the lock)."""
    user.password_hash = password_hash
    await revoke_tokens_locked(db, user.id)
    await db.commit()
    logger.warning("Password set via setup link for user %s", user.id)


async def log_setup_hint(email: str, log: logging.Logger) -> None:
    """Tell the operator how to get a setup link: once per boot, not once per
    uvicorn worker. Never the link itself."""
    try:
        from app.core.redis import get_redis

        redis = await get_redis()
        if not await redis.set("sinas:setup-link-hint", "1", nx=True, ex=60):
            return
    except Exception:
        pass  # no Redis: one line per worker beats none
    log.warning(
        f"Superadmin {email} has no password yet. Mint a one-time setup link "
        f"inside the backend container: {SETUP_LINK_COMMAND}"
    )
