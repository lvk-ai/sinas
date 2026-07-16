"""User schemas."""
import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class UserIdentityInput(BaseModel):
    """Link an external identity to a user."""

    provider: str = Field(..., min_length=1, max_length=255)
    subject: str = Field(..., min_length=1, max_length=255)
    metadata: Optional[dict[str, Any]] = None


class UserIdentityResponse(BaseModel):
    provider: str
    subject: str
    metadata: Optional[dict[str, Any]] = None
    last_synced_at: Optional[datetime] = None
    created_at: datetime


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    last_login_at: Optional[datetime]
    created_at: datetime
    custom_fields: Optional[dict[str, Any]] = None

    class Config:
        from_attributes = True


class UserWithRolesResponse(BaseModel):
    id: uuid.UUID
    email: str
    last_login_at: Optional[datetime] = None
    created_at: datetime
    roles: list[str]
    custom_fields: Optional[dict[str, Any]] = None
    identities: list[UserIdentityResponse] = []

    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    # When provided, replaces the entire custom_fields object
    custom_fields: Optional[dict[str, Any]] = None
