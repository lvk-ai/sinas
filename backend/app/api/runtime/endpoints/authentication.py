"""Authentication endpoints."""

import html as html_lib
import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    RESET_LINK,
    apply_new_password,
    create_access_token,
    create_otp_session,
    create_refresh_token,
    get_current_user,
    get_current_user_with_permissions,
    get_user_by_email,
    hash_password,
    normalize_email,
    revoke_all_refresh_tokens,
    revoke_outstanding_password_reset_tokens,
    revoke_refresh_token,
    revoke_tokens_locked,
    set_permission_used,
    take_reset_token,
    validate_refresh_token,
    verify_otp_code,
    verify_password,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.permissions import check_permission
from app.core.rate_limit import rate_limit_by_ip, rate_limit_by_value
from app.models import User
from app.models.user import Role, UserRole
from app.schemas.auth import (
    AuthUserResponse,
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    OTPVerifyRequest,
    OTPVerifyResponse,
    PermissionCheckRequest,
    PermissionCheckResponse,
    PermissionCheckResult,
    RefreshRequest,
    RefreshResponse,
    ResetPasswordRequest,
    SetupLinkCompleteRequest,
    SetupLinkExchangeRequest,
    SetupLinkExchangeResponse,
    TokenExchangeRequest,
    TokenExchangeResponse,
)
from app.services import setup_link as setup_link_service
from app.services import sso as sso_service
from app.services.user_identity import (
    IdentityConflictError,
    get_user_by_identity,
    link_user_identity,
)


# Verified in place of a real hash for unknown accounts so login timing does
# not reveal whether an email exists.
_DUMMY_PASSWORD_HASH = hash_password("enumeration-resistant-dummy")


async def _issue_tokens(db: AsyncSession, user: User, **refresh_options) -> tuple[str, str, AuthUserResponse]:
    """Mint an access + refresh token pair and build the user response.
    `refresh_options` go to create_refresh_token (an SSO session's cap,
    which the access token never outlives either)."""
    cap = refresh_options.get("max_lifetime")
    usual = timedelta(minutes=settings.access_token_expire_minutes)
    access_token = create_access_token(
        user_id=str(user.id), email=user.email, expires_delta=cap if cap is not None and cap < usual else None
    )
    refresh_token_plain, _ = await create_refresh_token(db, str(user.id), **refresh_options)

    roles_result = await db.execute(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id, UserRole.active == True)
    )
    role_names = [r[0] for r in roles_result.all()]

    user_resp = AuthUserResponse.model_validate(user)
    user_resp.roles = role_names
    return access_token, refresh_token_plain, user_resp

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/login", response_model=LoginResponse)
async def login(request: LoginRequest, http_request: Request, db: AsyncSession = Depends(get_db)):
    """
    Initiate login. Behavior depends on AUTH_MODE:

    - otp:          email only → sends OTP, returns session_id
    - password:     email + password → returns tokens immediately (session_id=None)
    - password+otp: email + password → verifies password, sends OTP, returns session_id

    User must exist — users are created by admins only. Responses never reveal
    whether an email has an account (anti-enumeration).
    """
    await rate_limit_by_ip(http_request, "login", settings.rate_limit_login_ip_max, settings.rate_limit_window_seconds)
    await rate_limit_by_value(request.email, "login:email", settings.rate_limit_login_email_max, settings.rate_limit_window_seconds)

    # A domain an identity provider enforces signs in there (never the
    # superadmin). By domain, so it says nothing about whether the account
    # exists.
    sso = await sso_service.enforcing_provider(db, request.email)
    if sso is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=sso_service.enforced_message(sso, request.email),
        )

    auth_mode = settings.auth_mode
    requires_password = "password" in auth_mode
    requires_otp = "otp" in auth_mode

    if requires_password and not request.password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password required for this auth mode.",
        )

    # Check if user exists and is active - no auto-provisioning. To prevent
    # account enumeration, unknown and deactivated (soft-deleted) emails must
    # get responses indistinguishable from real accounts: a decoy session in
    # OTP-only mode, the generic 401 in password modes.
    result = await db.execute(select(User).where(User.email == normalize_email(request.email)))
    user = result.scalar_one_or_none()
    valid_account = user is not None and user.is_active

    # Verify password before any OTP send so we don't email on bad credentials.
    # Unknown accounts and accounts without a password verify against a dummy
    # hash to keep response timing uniform, then get the same generic 401.
    if requires_password:
        real_hash = user.password_hash if valid_account else None
        password_ok = verify_password(request.password or "", real_hash or _DUMMY_PASSWORD_HASH)
        if not (real_hash and password_ok):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password.",
            )

    if requires_otp:
        if not valid_account:
            # Decoy: no email is sent; verifying any code against this session
            # id fails with the same "Invalid or expired OTP" as a wrong code.
            return LoginResponse(message="OTP sent to your email", session_id=uuid.uuid4())
        try:
            otp_session = await create_otp_session(db, request.email)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Failed to send OTP email: {str(e)}",
            )
        return LoginResponse(message="OTP sent to your email", session_id=otp_session.id)

    # password-only mode: issue tokens immediately
    access_token, refresh_token, user_resp = await _issue_tokens(db, user)
    return LoginResponse(
        message="Login successful",
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        user=user_resp,
    )


@router.post("/verify-otp", response_model=OTPVerifyResponse)
async def verify_otp(request: OTPVerifyRequest, http_request: Request, db: AsyncSession = Depends(get_db)):
    """
    Verify OTP and return access + refresh tokens.

    Returns short-lived access token (15 min) and long-lived refresh token (30 days).
    """
    await rate_limit_by_ip(http_request, "verify_otp", settings.rate_limit_otp_ip_max, settings.rate_limit_window_seconds)

    # Verify OTP
    otp_session = await verify_otp_code(db, str(request.session_id), request.otp_code)

    if not otp_session:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired OTP"
        )

    # Get user (must exist and be active - checked during login, re-checked here
    # in case the user was deactivated between OTP send and verification)
    user = await get_user_by_email(db, otp_session.email)

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User not found. Contact your administrator.",
        )

    # SSO enforced since the code was sent.
    sso = await sso_service.enforcing_provider(db, user.email)
    if sso is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=sso_service.enforced_message(sso, user.email),
        )

    access_token, refresh_token, user_resp = await _issue_tokens(db, user)

    return OTPVerifyResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        user=user_resp,
    )


@router.post("/token/exchange", response_model=TokenExchangeResponse)
async def token_exchange(
    request: Request,
    exchange: TokenExchangeRequest,
    current_user_data=Depends(get_current_user_with_permissions),
    db: AsyncSession = Depends(get_db),
):
    """
    Exchange an external identity for Sinas tokens (RFC 8693 shape).

    A trusted partner backend — authenticated with an API key holding
    sinas.auth.exchange:all — asserts that it has authenticated the user
    identified by (provider, subject) and receives a Sinas access + refresh
    token pair for that user. With auto_provision, unknown users are created
    with the configured default role.

    The caller is trusted to assert identities: guard the exchange permission
    like any other admin credential.
    """
    _, permissions = current_user_data

    if not check_permission(permissions, "sinas.auth.exchange:all"):
        set_permission_used(request, "sinas.auth.exchange:all", has_perm=False)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to exchange tokens",
        )

    set_permission_used(request, "sinas.auth.exchange:all")

    provisioned = False
    user = await get_user_by_identity(db, exchange.provider, exchange.subject)

    # Fallback: link the identity to an existing user matched by email
    if not user and exchange.email:
        user = await get_user_by_email(db, exchange.email)

    if not user:
        if not exchange.auto_provision:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No user linked to this identity",
            )
        if not exchange.email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="email is required to auto-provision a user",
            )

        user = User(
            email=normalize_email(exchange.email),
            custom_fields=exchange.custom_fields,
        )
        db.add(user)
        await db.flush()

        default_role = await Role.get_by_name(db, settings.token_exchange_default_role)
        if default_role:
            db.add(UserRole(role_id=default_role.id, user_id=user.id, active=True))
        provisioned = True
    elif exchange.custom_fields:
        # Shallow merge: partner-provided keys win, admin-set keys survive
        user.custom_fields = {**(user.custom_fields or {}), **exchange.custom_fields}

    try:
        await link_user_identity(
            db, user, exchange.provider, exchange.subject, exchange.metadata
        )
    except IdentityConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    user.last_login_at = datetime.now(UTC)

    access_token, refresh_token, user_resp = await _issue_tokens(db, user)

    return TokenExchangeResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        user=user_resp,
        provisioned=provisioned,
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_access_token(request: RefreshRequest, db: AsyncSession = Depends(get_db)):
    """
    Refresh access token using refresh token.

    Exchange a valid refresh token for a new short-lived access token.
    Refresh token remains valid and can be reused until expiry or logout.
    """
    # Validate refresh token
    result = await validate_refresh_token(db, request.refresh_token)

    if not result:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token"
        )

    user_id, email = result

    # A password or code session ends once the user's domain must use SSO.
    session = await sso_service.refresh_session(db, request.refresh_token)
    blocked = await sso_service.refresh_blocked(db, email, session)
    if blocked:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=blocked)

    # Create new access token; an SSO session's never outlives the session.
    lifetime = sso_service.access_token_lifetime(session)
    access_token = create_access_token(user_id=user_id, email=email, expires_delta=lifetime)

    return RefreshResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=int(lifetime.total_seconds()) if lifetime else settings.access_token_expire_minutes * 60,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: LogoutRequest, db: AsyncSession = Depends(get_db)):
    """
    Logout by revoking refresh token.

    After logout, the refresh token can no longer be used to get new access tokens.
    Existing access tokens remain valid until they expire (max 15 minutes).
    """
    success = await revoke_refresh_token(db, request.refresh_token)

    if not success:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Refresh token not found")

    return None


@router.get("/me", response_model=AuthUserResponse)
async def get_current_user_info(
    request: Request, user_id: str = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Get current authenticated user info with roles."""
    set_permission_used(request, "sinas.users.read:own")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Get user's roles (single query with join)
    roles_result = await db.execute(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id, UserRole.active == True)
    )
    role_names = [r[0] for r in roles_result.all()]

    resp = AuthUserResponse.model_validate(user)
    resp.roles = role_names
    return resp


@router.post("/password-reset", status_code=status.HTTP_204_NO_CONTENT)
async def redeem_password_reset(
    request: ResetPasswordRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Redeem a one-time password reset token (issued by an admin) and set a new
    password. Revokes all existing refresh tokens so other sessions are forced
    to re-authenticate.

    Only available when AUTH_MODE includes password.
    """
    if "password" not in settings.auth_mode:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password reset is not available in OTP-only auth mode.",
        )

    await rate_limit_by_ip(
        http_request,
        "password_reset",
        settings.rate_limit_login_ip_max,
        settings.rate_limit_window_seconds,
    )

    invalid = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid, expired, or already-used reset token.",
    )
    password_hash = hash_password(request.new_password)  # slow: not under the lock
    # Consume the link (links only, never a setup session) and set the
    # password in one transaction under the user lock, every other
    # outstanding link/session revoked with it.
    record = await take_reset_token(db, request.reset_token, RESET_LINK)
    if not record:
        raise invalid

    result = await db.execute(select(User).where(User.id == record.user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        await db.commit()  # the link stays spent
        raise invalid

    user.password_hash = password_hash
    await revoke_tokens_locked(db, user.id)
    await db.commit()
    await revoke_all_refresh_tokens(db, str(user.id))
    return None


_SETUP_LINK_OTP_ONLY = (
    "Setup links aren't used on this instance: sign in with the code emailed to you."
)


def _attribute(http_request: Request, user: User) -> None:
    """Credit the request log (the audit trail) to the user the link was for;
    these calls carry no bearer token to do it."""
    http_request.state.user_id = str(user.id)
    http_request.state.user_email = user.email


def _setup_link_refused(detail: str = setup_link_service.INVALID_LINK_MESSAGE) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


@router.post("/setup-link/exchange", response_model=SetupLinkExchangeResponse)
async def exchange_setup_link(
    request: SetupLinkExchangeRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Trade the token from a setup link (`<console>/ui/?token=...`) for a
    short-lived setup session. The link is spent by this call, so it can't be
    replayed from browser history or a log; the setup session token returned
    here lets the holder choose a password via POST /auth/setup-link/complete.

    Never signs anyone in. Every unusable link (unknown, expired, used,
    superseded, or for a deactivated account) gets the same 400. Only
    available when AUTH_MODE includes password.
    """
    if not setup_link_service.password_auth_enabled():
        raise _setup_link_refused(_SETUP_LINK_OTP_ONLY)

    await rate_limit_by_ip(
        http_request,
        "setup_link",
        settings.rate_limit_setup_link_ip_max,
        settings.rate_limit_window_seconds,
    )

    exchanged = await setup_link_service.exchange_setup_link(db, request.token)
    if exchanged is None:
        raise _setup_link_refused()
    session_token, session, user = exchanged
    _attribute(http_request, user)

    # A domain that must use SSO never signs in with a password.
    sso = await sso_service.enforcing_provider(db, user.email)
    if sso is not None:
        await revoke_outstanding_password_reset_tokens(db, user.id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=sso_service.enforced_message(sso, user.email),
        )

    return SetupLinkExchangeResponse(
        setup_token=session_token,
        email=user.email,
        has_password=bool(user.password_hash),
        expires_at=session.expires_at,
    )


@router.post("/setup-link/complete", response_model=LoginResponse)
async def complete_setup_link(
    request: SetupLinkCompleteRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Set a password with a setup session from POST /auth/setup-link/exchange,
    then sign in the way a correct password does in this AUTH_MODE:

    - password:     tokens are returned immediately
    - password+otp: a code is emailed and `session_id` returned; finish with
                    POST /auth/verify-otp (the link never skips the second factor)

    The setup session is single use. Every other outstanding link for the
    account and all of its refresh tokens are revoked.
    """
    if not setup_link_service.password_auth_enabled():
        raise _setup_link_refused(_SETUP_LINK_OTP_ONLY)

    await rate_limit_by_ip(
        http_request,
        "setup_link_complete",
        settings.rate_limit_setup_link_ip_max,
        settings.rate_limit_window_seconds,
    )

    password_hash = hash_password(request.new_password)  # slow: not under the lock
    # Consumes the session (sessions only, never a link) under the user lock
    # and keeps the transaction open: the password is set in it, so nothing
    # can change the password or revoke the session in between.
    user = await setup_link_service.redeem_setup_session(db, request.setup_token)
    if user is None:
        raise _setup_link_refused()
    _attribute(http_request, user)

    sso = await sso_service.enforcing_provider(db, user.email)
    if sso is not None:
        await db.commit()  # the session stays spent
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=sso_service.enforced_message(sso, user.email),
        )

    await setup_link_service.finish_setup(db, user, password_hash)
    await revoke_all_refresh_tokens(db, str(user.id))

    if "otp" in settings.auth_mode:
        try:
            otp_session = await create_otp_session(db, user.email)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Your password is set, but the sign-in code could not be sent: {e}",
            )
        return LoginResponse(message="OTP sent to your email", session_id=otp_session.id)

    access_token, refresh_token, user_resp = await _issue_tokens(db, user)
    return LoginResponse(
        message="Password set",
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        user=user_resp,
    )


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    request: ChangePasswordRequest,
    http_request: Request,
    user_id: str = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Authenticated user changes their own password. Verifies current password,
    sets new password, and revokes all other refresh tokens.

    Only available when AUTH_MODE includes password.
    """
    if "password" not in settings.auth_mode:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password change is not available in OTP-only auth mode.",
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if not user.password_hash or not verify_password(
        request.current_password, user.password_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect.",
        )

    # Under the user lock; any outstanding link/setup session dies with it.
    await apply_new_password(db, user, hash_password(request.new_password))
    await revoke_all_refresh_tokens(db, str(user.id))
    return None


@router.post("/check-permissions", response_model=PermissionCheckResponse)
async def check_permissions(
    request: Request,
    check_request: PermissionCheckRequest,
    current_user_data=Depends(get_current_user_with_permissions),
):
    """
    Check if the authenticated user has specific permission(s).

    Supports AND/OR logic:
    - AND: User must have ALL specified permissions
    - OR: User must have AT LEAST ONE of the specified permissions

    Example request:
    ```json
    {
        "permissions": ["sinas.functions.read:all", "sinas.functions.create:own"],
        "logic": "OR"
    }
    ```
    """
    user_id, permissions = current_user_data

    # Check each permission and log the check
    checks = []
    for perm in check_request.permissions:
        has_perm = check_permission(permissions, perm)
        set_permission_used(request, perm, has_perm=has_perm)
        checks.append(PermissionCheckResult(permission=perm, has_permission=has_perm))

    # Calculate overall result based on logic
    if check_request.logic == "AND":
        result = all(check.has_permission for check in checks)
    else:  # OR
        result = any(check.has_permission for check in checks)

    return PermissionCheckResponse(result=result, logic=check_request.logic, checks=checks)


def _oauth_result_page(ok: bool, message: str) -> HTMLResponse:
    """Render a tiny page that notifies the opener window and closes itself.

    The connector OAuth flow is opened in a popup; this signals the console (via
    postMessage) whether the connection succeeded, then closes. No secrets are sent.
    The message is targeted at the console's own origin (not "*") so it can't be read by
    an unrelated opener, and the console-side listener validates event.origin in turn.
    """
    status_word = "success" if ok else "error"
    safe = html_lib.escape(message)
    # The message carries no secret (just a status flag), and the console/API can be on
    # different origins in local dev, so we post to "*". Spoofing is prevented on the
    # RECEIVER side: the console validates event.origin against the API origin before
    # trusting the message (see ConnectorEditor's message listener).
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Connector authorization</title></head>
<body style="font-family:system-ui;background:#0d0d0d;color:#e5e5e5;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center">
<p style="font-size:15px">{safe}</p>
<p style="font-size:13px;color:#888">You can close this window.</p>
</div>
<script>
  try {{ window.opener && window.opener.postMessage(
    {{ type: "connector-oauth", status: "{status_word}" }}, "*"); }} catch (e) {{}}
  setTimeout(function () {{ window.close(); }}, 1200);
</script>
</body></html>"""
    return HTMLResponse(content=html, status_code=200 if ok else 400)


@router.get("/connectors/oauth/callback", include_in_schema=False)
async def connector_oauth_callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    error_description: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
):
    """Public OAuth redirect target. Exchanges the code for tokens for the initiating user.

    Unauthenticated by design (the provider won't send our bearer). The initiating user and
    connector are recovered from the single-use `state` minted at /connectors/.../oauth/authorize.
    """
    from app.core.oauth_state import BIND_COOKIE_NAME, consume_state
    from app.models.connector import Connector
    from app.services.connector_service import connector_service

    if error:
        return _oauth_result_page(False, f"Authorization was denied: {error_description or error}")

    ctx = await consume_state(state)
    if ctx is None:
        return _oauth_result_page(False, "This authorization link has expired or was already used.")

    # Browser-binding check: the flow must be completed by the same browser that started
    # it. The nonce was set as an HttpOnly cookie by the authenticated begin endpoint;
    # require it to match the one stored with the state. This defeats OAuth account-linking
    # (an attacker minting a state and having a victim complete it). Constant-time compare.
    # Skippable only in split-origin local dev (see settings.oauth_bind_browser_session),
    # where the cookie can't round-trip cross-origin.
    cookie_nonce = request.cookies.get(BIND_COOKIE_NAME) or ""
    expected_nonce = ctx.get("browser_nonce") or ""
    binding_ok = bool(expected_nonce) and secrets.compare_digest(cookie_nonce, expected_nonce)
    if settings.oauth_bind_browser_session and not binding_ok:
        resp = _oauth_result_page(
            False,
            "This authorization could not be verified for your session. "
            "Please start the connection again from the connector page.",
        )
        resp.delete_cookie(BIND_COOKIE_NAME, path="/")
        return resp
    if not binding_ok:
        logger.warning(
            "OAuth browser-binding check skipped (oauth_bind_browser_session=false) — "
            "acceptable only for local single-user dev, never production."
        )

    if not code:
        return _oauth_result_page(False, "No authorization code was returned by the provider.")

    connector = await Connector.get_by_name(db, ctx["namespace"], ctx["name"])
    if connector is None or (connector.auth or {}).get("type") != "oauth2_authorization_code":
        return _oauth_result_page(False, "Connector is no longer configured for OAuth.")

    ok = await connector_service.exchange_authorization_code(
        db=db, connector=connector, user_id=ctx["user_id"], code=code, code_verifier=ctx["code_verifier"],
    )
    if ok:
        # Re-credentialing is the recovery point for perUser pipelines that
        # skip-listed this user after consecutive failures.
        from app.services.pipeline_runner import reset_user_failure_state

        await reset_user_failure_state(db, ctx["user_id"], connector_id=connector.id)
    result = _oauth_result_page(
        ok,
        f"Connected {connector.namespace}/{connector.name}." if ok
        else "Failed to exchange the authorization code for a token.",
    )
    result.delete_cookie(BIND_COOKIE_NAME, path="/")
    return result
