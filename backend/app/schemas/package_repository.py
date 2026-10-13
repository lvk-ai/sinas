"""REST shapes for package repositories and the package catalog."""
import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class PackageRepositoryAuth(BaseModel):
    type: str = "none"  # none | bearer | header
    secret: Optional[str] = None  # the name of a shared Secret
    header: Optional[str] = None


class PackageRepositoryCreate(BaseModel):
    name: str
    url: str
    description: Optional[str] = None
    auth: PackageRepositoryAuth = Field(default_factory=PackageRepositoryAuth)
    is_active: bool = True


class PackageRepositoryUpdate(BaseModel):
    """Fields left out (or null) are unchanged; auth is replaced whole. The
    name can't change: installed packages follow a repository by name (add a
    new one and remove the old instead)."""

    model_config = {"extra": "forbid"}

    url: Optional[str] = None
    description: Optional[str] = None
    auth: Optional[PackageRepositoryAuth] = None
    is_active: Optional[bool] = None


class PackageRepositoryResponse(BaseModel):
    id: uuid.UUID
    name: str
    url: str
    description: Optional[str]
    auth: dict[str, Any]
    is_active: bool
    managed_by: Optional[str]
    last_refreshed_at: Optional[datetime]
    last_error: Optional[str]
    package_count: int = 0
    created_at: datetime
    updated_at: Optional[datetime]

    @classmethod
    def of(cls, row) -> "PackageRepositoryResponse":
        return cls(
            id=row.id, name=row.name, url=row.url, description=row.description,
            auth=row.auth or {"type": "none"}, is_active=row.is_active is not False,
            managed_by=row.managed_by, last_refreshed_at=row.last_refreshed_at,
            last_error=row.last_error, package_count=len((row.index or {}).get("packages", [])),
            created_at=row.created_at, updated_at=row.updated_at,
        )


class CatalogInstall(BaseModel):
    name: str  # the install name (differs from the package name for a named instance)
    version: str
    update_available: bool


class CatalogEntry(BaseModel):
    repository: str
    name: str
    version: str
    description: Optional[str] = None
    author: Optional[str] = None
    readme_url: Optional[str] = None
    multi_instance: bool = False  # the index says it may be installed more than once
    installed: list[CatalogInstall] = Field(default_factory=list)
    update_available: bool = False
