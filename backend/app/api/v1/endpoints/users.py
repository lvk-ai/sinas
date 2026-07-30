"""User management endpoints."""
import uuid
from datetime import UTC, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import (
    create_password_reset_token,
    get_current_user_with_permissions,
    normalize_email,
    set_permission_used,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.permissions import check_permission
from app.models.user import APIKey, RefreshToken, Role, User, UserIdentity, UserRole
from app.schemas import UserResponse, UserUpdate
from app.schemas.auth import AdminCreateResetLinkResponse, CreateUserRequest
from app.schemas.user import UserIdentityInput, UserIdentityResponse, UserWithRolesResponse
from app.services.user_identity import (
    IdentityConflictError,
    get_user_by_identity,
    link_user_identity,
)

router = APIRouter(prefix="/users", tags=["users"])


async def _user_with_roles_response(db: AsyncSession, user: User) -> UserWithRolesResponse:
    """Build the full user response with role names and identities."""
    roles_result = await db.execute(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id, UserRole.active == True)
    )
    identities_result = await db.execute(
        select(UserIdentity).where(UserIdentity.user_id == user.id)
    )
    return UserWithRolesResponse(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        roles=list(roles_result.scalars().all()),
        custom_fields=user.custom_fields,
        identities=_identity_responses(list(identities_result.scalars().all())),
    )


def _identity_responses(identities: list[UserIdentity]) -> list[UserIdentityResponse]:
    return [
        UserIdentityResponse(
            provider=i.provider,
            subject=i.subject,
            metadata=i.identity_metadata,
            last_synced_at=i.last_synced_at,
            created_at=i.created_at,
        )
        for i in identities
    ]


@router.get("", response_model=list[UserWithRolesResponse])
async def list_users(
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    search: Optional[str] = Query(None, description="Search by email"),
    db: AsyncSession = Depends(get_db),
    current_user_data=Depends(get_current_user_with_permissions),
):
    """List users with their roles. Only admins can list all users."""
    user_id, permissions = current_user_data

    # Only admins can list users
    if not check_permission(permissions, "sinas.users.read:all"):
        set_permission_used(request, "sinas.users.read:all", has_perm=False)
        raise HTTPException(status_code=403, detail="Not authorized to list users")

    set_permission_used(request, "sinas.users.read:all")

    query = select(User).options(selectinload(User.identities))
    if search:
        query = query.where(User.email.ilike(f"%{search}%"))
    query = query.order_by(User.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    users = result.scalars().all()

    # Batch-load role names for all users
    user_ids = [u.id for u in users]
    if user_ids:
        memberships_result = await db.execute(
            select(UserRole.user_id, Role.name)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id.in_(user_ids), UserRole.active == True)
        )
        roles_by_user: dict[uuid.UUID, list[str]] = {}
        for uid, role_name in memberships_result.all():
            roles_by_user.setdefault(uid, []).append(role_name)
    else:
        roles_by_user = {}

    return [
        UserWithRolesResponse(
            id=u.id,
            email=u.email,
            is_active=u.is_active,
            last_login_at=u.last_login_at,
            created_at=u.created_at,
            roles=roles_by_user.get(u.id, []),
            custom_fields=u.custom_fields,
            identities=_identity_responses(u.identities),
        )
        for u in users
    ]


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    request: Request,
    user_request: CreateUserRequest,
    current_user_data: tuple = Depends(get_current_user_with_permissions),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new user by email address.

    Only admins can create users. New users are assigned to the GuestUsers role by default.
    Requires permission: sinas.users.post:all
    """
    user_id, permissions = current_user_data

    # Check admin permission
    if not check_permission(permissions, "sinas.users.create:all"):
        set_permission_used(request, "sinas.users.create:all", has_perm=False)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to create users"
        )

    set_permission_used(request, "sinas.users.create:all")

    # Check if user already exists
    normalized_email = normalize_email(user_request.email)

    result = await db.execute(select(User).where(User.email == normalized_email))
    user = result.scalar_one_or_none()

    if user and user.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User with email '{user_request.email}' already exists",
        )

    if user:
        # Reactivate a soft-deleted user (role memberships were deactivated on
        # delete, so they fall through to the GuestUsers assignment below)
        user.is_active = True
        await db.flush()
    else:
        # Create new user
        user = User(email=normalized_email, custom_fields=user_request.custom_fields)
        db.add(user)
        await db.flush()

        # Link external identities (new users only; a reactivated account keeps its
        # existing identity links)
        for identity in user_request.identities:
            try:
                await link_user_identity(
                    db, user, identity.provider, identity.subject, identity.metadata
                )
            except IdentityConflictError as e:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    # Check if already has active roles
    memberships_result = await db.execute(
        select(UserRole).where(UserRole.user_id == user.id, UserRole.active == True)
    )
    existing_memberships = memberships_result.scalars().all()

    # Only add to GuestUsers if no roles assigned yet
    if not existing_memberships:
        guest_role_result = await db.execute(select(Role).where(Role.name == "GuestUsers"))
        guest_role = guest_role_result.scalar_one_or_none()

        if guest_role:
            membership = UserRole(role_id=guest_role.id, user_id=user.id, active=True)
            db.add(membership)
            await db.flush()
            await db.refresh(user)

    return UserResponse.model_validate(user)


@router.get("/by-identity", response_model=UserWithRolesResponse)
async def get_user_by_external_identity(
    request: Request,
    provider: str = Query(..., min_length=1),
    subject: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
    current_user_data=Depends(get_current_user_with_permissions),
):
    """Look up a user by external identity (provider + subject). Admin only."""
    _, permissions = current_user_data

    if not check_permission(permissions, "sinas.users.read:all"):
        set_permission_used(request, "sinas.users.read:all", has_perm=False)
        raise HTTPException(status_code=403, detail="Not authorized to look up users")

    set_permission_used(request, "sinas.users.read:all")

    user = await get_user_by_identity(db, provider, subject)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return await _user_with_roles_response(db, user)


@router.get("/{user_id}", response_model=UserWithRolesResponse)
async def get_user(
    request: Request,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user_data=Depends(get_current_user_with_permissions),
):
    """Get a specific user with their roles."""
    current_user_id, permissions = current_user_data

    # Use mixin for permission-aware get
    user = await User.get_with_permissions(
        db=db,
        user_id=current_user_id,
        permissions=permissions,
        action="read",
        resource_id=user_id,
    )

    set_permission_used(request, "sinas.users.read")

    return await _user_with_roles_response(db, user)


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    request: Request,
    user_id: uuid.UUID,
    user_data: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user_data=Depends(get_current_user_with_permissions),
):
    """Update a user. Only admins can update other users."""
    current_user_id, permissions = current_user_data

    # Use mixin for permission-aware get
    user = await User.get_with_permissions(
        db=db,
        user_id=current_user_id,
        permissions=permissions,
        action="update",
        resource_id=user_id,
    )

    set_permission_used(request, "sinas.users.update")

    if user_data.custom_fields is not None:
        user.custom_fields = user_data.custom_fields

    await db.flush()
    await db.refresh(user)

    return user


@router.post(
    "/{user_id}/identities",
    response_model=UserIdentityResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_user_identity(
    request: Request,
    user_id: uuid.UUID,
    identity_data: UserIdentityInput,
    db: AsyncSession = Depends(get_db),
    current_user_data=Depends(get_current_user_with_permissions),
):
    """Link an external identity to a user. Admin only."""
    _, permissions = current_user_data

    if not check_permission(permissions, "sinas.users.update:all"):
        set_permission_used(request, "sinas.users.update:all", has_perm=False)
        raise HTTPException(status_code=403, detail="Not authorized to manage identities")

    set_permission_used(request, "sinas.users.update:all")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        identity = await link_user_identity(
            db, user, identity_data.provider, identity_data.subject, identity_data.metadata
        )
    except IdentityConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    return UserIdentityResponse(
        provider=identity.provider,
        subject=identity.subject,
        metadata=identity.identity_metadata,
        last_synced_at=identity.last_synced_at,
        created_at=identity.created_at,
    )


@router.delete("/{user_id}/identities", status_code=status.HTTP_204_NO_CONTENT)
async def remove_user_identity(
    request: Request,
    user_id: uuid.UUID,
    provider: str = Query(..., min_length=1),
    subject: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
    current_user_data=Depends(get_current_user_with_permissions),
):
    """Unlink an external identity from a user. Admin only."""
    _, permissions = current_user_data

    if not check_permission(permissions, "sinas.users.update:all"):
        set_permission_used(request, "sinas.users.update:all", has_perm=False)
        raise HTTPException(status_code=403, detail="Not authorized to manage identities")

    set_permission_used(request, "sinas.users.update:all")

    result = await db.execute(
        select(UserIdentity).where(
            UserIdentity.user_id == user_id,
            UserIdentity.provider == provider,
            UserIdentity.subject == subject,
        )
    )
    identity = result.scalar_one_or_none()
    if not identity:
        raise HTTPException(status_code=404, detail="Identity not found")

    await db.delete(identity)
    await db.flush()

    return None


@router.post("/{user_id}/password-reset", response_model=AdminCreateResetLinkResponse)
async def admin_create_password_reset(
    request: Request,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user_data=Depends(get_current_user_with_permissions),
):
    """
    Admin generates a one-time password reset token for a user.

    The plaintext token is returned exactly once and should be delivered to the
    user out-of-band (Slack, in person, etc). The user redeems it via
    POST /auth/password-reset.

    Only available when AUTH_MODE includes password.
    """
    if "password" not in settings.auth_mode:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password reset is not available in OTP-only auth mode.",
        )

    current_user_id, permissions = current_user_data

    if not check_permission(permissions, "sinas.users.update:all"):
        set_permission_used(request, "sinas.users.update:all", has_perm=False)
        raise HTTPException(status_code=403, detail="Not authorized to reset passwords")

    set_permission_used(request, "sinas.users.update:all")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=404, detail="User not found")

    plain_token, record = await create_password_reset_token(
        db, str(user.id), created_by=current_user_id
    )
    return AdminCreateResetLinkResponse(
        user_id=user.id,
        reset_token=plain_token,
        expires_at=record.expires_at,
    )


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    request: Request,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user_data=Depends(get_current_user_with_permissions),
):
    """Delete a user. Only admins can delete users."""
    current_user_id, permissions = current_user_data

    # Prevent deleting yourself
    if str(user_id) == current_user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own user account")

    # Use mixin for permission-aware get
    user = await User.get_with_permissions(
        db=db,
        user_id=current_user_id,
        permissions=permissions,
        action="delete",
        resource_id=user_id,
    )

    # Prevent deleting the superadmin
    import os
    superadmin_email = os.environ.get("SUPERADMIN_EMAIL", "")
    if user.email and user.email.lower() == superadmin_email.lower():
        raise HTTPException(status_code=403, detail="Cannot delete the superadmin account")

    set_permission_used(request, "sinas.users.delete")

    # Soft delete: deactivate the user, remove all role memberships, and revoke
    # their credentials so existing API keys and refresh tokens stop working
    # immediately (access tokens die at expiry via the is_active check in auth)
    now = datetime.now(UTC)
    user.is_active = False

    result = await db.execute(
        select(UserRole).where(UserRole.user_id == user_id, UserRole.active == True)
    )
    for membership in result.scalars().all():
        membership.active = False
        membership.removed_at = now
        membership.removed_by = uuid.UUID(current_user_id)

    result = await db.execute(
        select(APIKey).where(APIKey.user_id == user_id, APIKey.is_active == True)
    )
    for api_key in result.scalars().all():
        api_key.is_active = False
        api_key.revoked_at = now
        api_key.revoked_by = uuid.UUID(current_user_id)

    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id, RefreshToken.is_revoked == False
        )
    )
    for token in result.scalars().all():
        token.is_revoked = True
        token.revoked_at = now

    await db.flush()

    return None
