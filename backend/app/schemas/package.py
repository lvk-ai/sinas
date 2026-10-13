"""Package schemas for installable integration packages."""
import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


INSTANCE_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"


def _one_source(model):
    if (model.source is None) == (model.repository is None and model.package is None):
        raise ValueError("Give either source (the YAML) or repository and package")
    if model.source is None and not (model.repository and model.package):
        raise ValueError("Installing from a repository needs both repository and package")
    return model


class PackageInstallRequest(BaseModel):
    """Request to install a package from YAML content, or from a package
    repository."""

    check_source = model_validator(mode="after")(_one_source)

    source: Optional[str] = Field(None, description="YAML content of the SinasPackage")
    # Or, instead of `source`: a package from a package repository, which
    # Sinas fetches and checks against the repository's index.
    repository: Optional[str] = Field(None, description="Package repository to install from")
    package: Optional[str] = Field(None, description="Package name in that repository")
    variables: Optional[dict[str, Any]] = Field(None, description="Install-time variable values")
    instance: Optional[str] = Field(
        None,
        pattern=INSTANCE_NAME_PATTERN,
        description=(
            "Install name for a package with package.multiInstance: true, so the same "
            "package can be installed more than once (e.g. records-work, records-personal). "
            "Substituted for ${{ install.name }} in the YAML. Defaults to the package name."
        ),
    )
    allowBroadRolePermissions: bool = Field(
        False,
        description=(
            "Accept package roles whose granted permissions reach outside the "
            "package's own namespaces (listed by preview). Off by default."
        ),
    )
    adoptUnmanaged: bool = Field(
        False,
        description=(
            "Take over resources the package declares that exist but were created or "
            "edited by hand (skipped otherwise): their hand edits are overwritten, and "
            "uninstalling the package deletes them. Resources another package or "
            "config manages are still skipped. Off by default."
        ),
    )


class PackagePreviewRequest(BaseModel):
    """Request to preview a package install (dry run)."""

    check_source = model_validator(mode="after")(_one_source)

    source: Optional[str] = Field(None, description="YAML content of the SinasPackage")
    repository: Optional[str] = Field(None, description="Package repository to preview from")
    package: Optional[str] = Field(None, description="Package name in that repository")
    variables: Optional[dict[str, Any]] = Field(None, description="Variable values for substitution")
    instance: Optional[str] = Field(
        None, pattern=INSTANCE_NAME_PATTERN,
        description="Install name to preview under (multi-instance packages)",
    )
    adoptUnmanaged: bool = Field(
        False,
        description=(
            "Preview the install with adoptUnmanaged: resources created or edited by "
            "hand are listed as updates the package takes over, each with a warning."
        ),
    )


class PackageResourceRef(BaseModel):
    """Reference to a resource for package creation."""
    type: str = Field(..., description="Resource type: agent, function, skill, app, component, query, collection, template, webhook, schedule")
    namespace: str = "default"
    name: str


class PackageCreateRequest(BaseModel):
    """Request to create a package YAML from selected resources."""
    name: str
    version: str = "1.0.0"
    description: Optional[str] = None
    author: Optional[str] = None
    url: Optional[str] = None
    resources: list[PackageResourceRef]


class PackageResponse(BaseModel):
    """Full package details."""
    id: uuid.UUID
    name: str
    package_name: Optional[str] = None   # declared name; differs from name for a named instance
    version: str
    description: Optional[str]
    author: Optional[str]
    source_url: Optional[str]
    source_repository: Optional[str] = None
    # From the repository it follows (see package_repositories.package_status).
    latest_version: Optional[str] = None
    update_available: bool = False
    installed_by: uuid.UUID
    installed_at: datetime
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True


class PackageListResponse(BaseModel):
    """Package summary for list view."""
    id: uuid.UUID
    name: str
    package_name: Optional[str] = None
    version: str
    description: Optional[str]
    author: Optional[str]
    installed_at: datetime
    source_repository: Optional[str] = None
    latest_version: Optional[str] = None
    update_available: bool = False

    class Config:
        from_attributes = True


class PackageInstallResponse(BaseModel):
    """Response from package install."""
    package: dict[str, Any]
    apply: dict[str, Any]
