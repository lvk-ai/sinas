"""Authentication endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    consume_password_reset_token,
    create_access_token,
    create_otp_session,
    create_refresh_token,
    get_current_user,
    get_current_user_with_permissions,
    get_user_by_email,
    hash_password,
    normalize_email,
    revoke_all_refresh_tokens,
    revoke_refresh_token,
    set_permission_used,
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
)


# Verified in place of a real hash for unknown accounts so login timing does
# not reveal whether an email exists.
_DUMMY_PASSWORD_HASH = hash_password("enumeration-resistant-dummy")


async def _issue_tokens(db: AsyncSession, user: User) -> tuple[str, str, AuthUserResponse]:
    """Mint an access + refresh token pair and build the user response."""
    access_token = create_access_token(user_id=str(user.id), email=user.email)
    refresh_token_plain, _ = await create_refresh_token(db, str(user.id))

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

    access_token, refresh_token, user_resp = await _issue_tokens(db, user)

    return OTPVerifyResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        user=user_resp,
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

    # Create new access token
    access_token = create_access_token(user_id=user_id, email=email)

    return RefreshResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
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

    record = await consume_password_reset_token(db, request.reset_token)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid, expired, or already-used reset token.",
        )

    result = await db.execute(select(User).where(User.id == record.user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.password_hash = hash_password(request.new_password)
    await db.commit()

    await revoke_all_refresh_tokens(db, str(user.id))
    return None


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

    user.password_hash = hash_password(request.new_password)
    await db.commit()

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
