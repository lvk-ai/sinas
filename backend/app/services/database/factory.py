"""Factory for database schema services."""

from app.schemas.database_connection import normalize_connection_type

from .base import DatabaseSchemaService


def get_schema_service(connection_type: str) -> DatabaseSchemaService:
    """Return the appropriate schema service for the given connection type."""
    if normalize_connection_type(connection_type) == "postgresql":
        from .postgres import PostgresSchemaService

        return PostgresSchemaService()

    raise ValueError(f"Unsupported connection type: {connection_type}")
