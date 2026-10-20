"""Database connection schemas."""
import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

# The connection types Sinas can open. Only PostgreSQL has a driver (asyncpg).
# Any other type used to be opened with asyncpg all the same, failing oddly or
# not at all; a new type needs a driver behind DatabasePoolManager first.
# Enforced by DatabaseConnectionSpec (REST create/update and config apply),
# the config schema, and the pool for rows stored before that.
SUPPORTED_CONNECTION_TYPES: tuple[str, ...] = ("postgresql",)
# Spellings that already worked (the pool never looked at the type): kept
# working, and stored as the canonical name.
_CONNECTION_TYPE_ALIASES = {"postgres": "postgresql"}


def normalize_connection_type(value: str) -> str:
    """The canonical name of a connection type (not checked for support)."""
    value = value.strip().lower()
    return _CONNECTION_TYPE_ALIASES.get(value, value)


def validate_connection_type(value: Any) -> Any:
    """The canonical connection type; ValueError when Sinas can't open it."""
    if not isinstance(value, str):
        return value  # left to the field's own type check
    normalized = normalize_connection_type(value)
    if normalized not in SUPPORTED_CONNECTION_TYPES:
        raise ValueError(
            f"Unsupported connection type '{value}'. "
            f"Supported: {', '.join(SUPPORTED_CONNECTION_TYPES)}"
        )
    return normalized


class DatabaseConnectionCreate(BaseModel):
    """Schema for creating a new database connection."""

    name: str
    connection_type: str  # validated by DatabaseConnectionSpec
    host: str
    port: int
    database: str
    username: str
    password: Optional[str] = None
    ssl_mode: Optional[str] = None
    config: Optional[dict[str, Any]] = None
    read_only: bool = False


class DatabaseConnectionUpdate(BaseModel):
    """Schema for updating a database connection."""

    name: Optional[str] = None
    connection_type: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    ssl_mode: Optional[str] = None
    config: Optional[dict[str, Any]] = None
    is_active: Optional[bool] = None
    read_only: Optional[bool] = None


class DatabaseConnectionResponse(BaseModel):
    """Schema for database connection response (password excluded)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    connection_type: str
    host: str
    port: int
    database: str
    username: str
    ssl_mode: Optional[str] = None
    config: dict[str, Any]
    is_active: bool
    read_only: bool
    created_at: datetime
    updated_at: datetime


class DatabaseConnectionTestRequest(BaseModel):
    """Schema for testing a connection with raw params (before saving)."""

    connection_type: str
    host: str
    port: int
    database: str
    username: str
    password: Optional[str] = None
    ssl_mode: Optional[str] = None


class DatabaseConnectionTestResponse(BaseModel):
    """Response from testing a database connection."""

    success: bool
    message: str
    latency_ms: Optional[int] = None
