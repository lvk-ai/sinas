"""Package repositories: where Sinas finds packages to install and updates for
installed ones (ADR 2026-10-13-package-repositories).

A repository is a URL serving an `index.json` that lists its packages (name,
version, path, sha256). Sinas refreshes the index now and then and keeps the
last good copy here; installing from a repository fetches the package file
and checks it against the index's checksum.
"""
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at, updated_at, uuid_pk


class PackageRepository(Base):
    __tablename__ = "package_repositories"

    id: Mapped[uuid_pk]
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # {"type": "none" | "bearer" | "header", "secret": <Secret name>, "header": ...}
    auth: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("users.id"))

    # Refresh state: runtime, not config.
    index: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)  # the last good index.json
    last_refreshed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)

    managed_by: Mapped[Optional[str]] = mapped_column(String(255))
    config_name: Mapped[Optional[str]] = mapped_column(String(255))
    config_checksum: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[created_at]
    updated_at: Mapped[updated_at]
