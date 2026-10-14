"""LLM Provider schemas."""
import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class LLMProviderCreate(BaseModel):
    """Schema for creating a new LLM provider."""

    name: str
    provider_type: str  # "openai", "anthropic", "ollama", "azure", etc.
    api_key: Optional[str] = None
    api_endpoint: Optional[str] = None
    default_model: Optional[str] = None
    config: Optional[dict[str, Any]] = None
    is_default: Optional[bool] = False
    # Pricing (docs/design/usage-cost.md): {model: {input_cost_per_token,
    # output_cost_per_token, cache_read_input_token_cost, ...}}
    pricing: Optional[dict[str, dict[str, Any]]] = None
    model_aliases: Optional[dict[str, str]] = None
    discount: Optional[float] = None
    # The LiteLLM provider family to price models as (e.g. "mistral" for an
    # OpenAI-compatible Mistral endpoint); unset: the provider type's own.
    pricing_provider: Optional[str] = None


class LLMProviderUpdate(BaseModel):
    """Schema for updating an LLM provider."""

    name: Optional[str] = None
    provider_type: Optional[str] = None
    api_key: Optional[str] = None
    api_endpoint: Optional[str] = None
    default_model: Optional[str] = None
    config: Optional[dict[str, Any]] = None
    is_default: Optional[bool] = None
    is_active: Optional[bool] = None
    # Replaced as a whole; {} clears. discount and pricing_provider: null
    # clears them.
    pricing: Optional[dict[str, dict[str, Any]]] = None
    model_aliases: Optional[dict[str, str]] = None
    discount: Optional[float] = None
    pricing_provider: Optional[str] = None


class LLMProviderResponse(BaseModel):
    """Schema for LLM provider response (API key is never returned)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    provider_type: str
    api_endpoint: Optional[str] = None
    default_model: Optional[str] = None
    config: dict[str, Any]
    is_default: bool
    is_active: bool
    pricing: dict[str, dict[str, Any]] = Field(default_factory=dict)
    model_aliases: dict[str, str] = Field(default_factory=dict)
    discount: Optional[float] = Field(
        default=None, validation_alias=AliasChoices("discount", "pricing_discount")
    )
    pricing_provider: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @field_validator("pricing", "model_aliases", mode="before")
    @classmethod
    def _none_is_empty(cls, value: Any) -> Any:
        return {} if value is None else value
