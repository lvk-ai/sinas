from typing import Any, Optional

from sqlalchemy import JSON, Boolean, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at, updated_at, uuid_pk


class LLMProvider(Base):
    """
    Stores LLM provider configurations (API keys, endpoints, etc.)
    Replaces environment variables for LLM configuration.
    """

    __tablename__ = "llm_providers"

    id: Mapped[uuid_pk]
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    # Provider type: "openai", "anthropic", "ollama", "azure", etc.
    provider_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # API configuration (encrypted in database)
    api_key: Mapped[Optional[str]] = mapped_column(Text)  # Encrypted with ENCRYPTION_KEY
    # For custom endpoints. For provider_type "azure" this is the azure_endpoint
    # (e.g. https://<resource>.openai.azure.com/ or a gateway URL).
    api_endpoint: Mapped[Optional[str]] = mapped_column(String(500))
    default_model: Mapped[Optional[str]] = mapped_column(
        String(100)
    )  # Default model if not specified

    # Additional configuration (rate limits, organization_id, etc.)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Example: {"max_tokens": 128000, "organization_id": "org-..."}
    # Azure ("azure"/"azure_openai") reads from config:
    #   {"api_version": "2024-10-21",        # Azure API version
    #    "azure_deployment": "<deployment>", # optional: pin one deployment
    #    "max_tokens_param": "max_completion_tokens",  # token-limit param name
    #    "drop_params": ["temperature"],     # strip per-deployment (reasoning models)
    #    "extra_params": {"reasoning_effort": "medium"}}  # merged into requests

    # Default provider for new assistants
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[created_at]
    updated_at: Mapped[updated_at]

    # Config tracking
    managed_by: Mapped[Optional[str]] = mapped_column(Text)
    config_name: Mapped[Optional[str]] = mapped_column(Text)
    config_checksum: Mapped[Optional[str]] = mapped_column(Text)

    @classmethod
    async def get_by_name(cls, db: AsyncSession, name: str) -> Optional["LLMProvider"]:
        """Get LLM provider by name."""
        result = await db.execute(select(cls).where(cls.name == name, cls.is_active == True))
        return result.scalar_one_or_none()
