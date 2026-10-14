"""System schemas."""

from typing import Any

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Response for system health endpoint."""

    warnings: list[dict[str, Any]]
    services: list[dict[str, Any]]
    host: dict[str, Any]
    # "docker" | "kubernetes" — decides where services/stats come from.
    deployment: str = "docker"
    executors: dict[str, str] = {}
    # Set when the service list (or, with include_stats, CPU/memory) could
    # not be read in this deployment; the console shows it instead of data.
    services_unavailable_reason: str | None = None
    metrics_unavailable_reason: str | None = None


class ContainerRestartResponse(BaseModel):
    """Response from container restart."""

    status: str
    container: str
