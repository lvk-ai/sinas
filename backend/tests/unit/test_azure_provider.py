"""Unit tests for the Azure OpenAI provider construction.

These tests exercise client construction only (no network calls): they verify
that the Azure-specific parameters are wired through correctly and that the
provider inherits the OpenAI-compatible request handling.
"""
import pytest
from openai import AsyncAzureOpenAI

from app.providers import AzureOpenAIProvider, OpenAIProvider
from app.providers.azure_openai_provider import (
    DEFAULT_API_VERSION,
    DEFAULT_MAX_TOKENS_PARAM,
)


def _prepare(provider, **overrides):
    """Invoke the param-building hook with sensible defaults."""
    call = {
        "model": "dep",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.7,
        "max_tokens": 100,
        "tools": None,
        "kwargs": {},
    }
    call.update(overrides)
    return provider._prepare_params(**call)


def test_builds_async_azure_client_with_params():
    provider = AzureOpenAIProvider(
        api_key="secret",
        azure_endpoint="https://genai-nexus.api.corpinter.net/",
        api_version="2024-10-21",
        azure_deployment="gpt-4o",
    )

    assert isinstance(provider.client, AsyncAzureOpenAI)
    assert provider.api_version == "2024-10-21"
    assert provider.azure_deployment == "gpt-4o"
    # azure_endpoint is recorded as the base_url on the shared base interface.
    assert provider.base_url == "https://genai-nexus.api.corpinter.net/"


def test_defaults_api_version_when_omitted():
    provider = AzureOpenAIProvider(
        api_key="secret",
        azure_endpoint="https://example.openai.azure.com/",
    )

    assert provider.api_version == DEFAULT_API_VERSION
    assert provider.azure_deployment is None


def test_requires_azure_endpoint():
    with pytest.raises(ValueError, match="azure_endpoint"):
        AzureOpenAIProvider(api_key="secret", azure_endpoint=None)


def test_inherits_response_handling():
    # The Azure provider reuses OpenAIProvider's complete/stream/format/usage
    # logic; only client construction and param building differ.
    assert issubclass(AzureOpenAIProvider, OpenAIProvider)
    for method in ("complete", "stream", "format_tool_calls", "extract_usage"):
        assert getattr(AzureOpenAIProvider, method) is getattr(OpenAIProvider, method)


# ---------------------------------------------------------------------------
# Config-driven request params (no model-name detection in code)
# ---------------------------------------------------------------------------


def _provider(**config):
    return AzureOpenAIProvider(
        api_key="secret",
        azure_endpoint="https://example.openai.azure.com/",
        max_tokens_param=config.get("max_tokens_param"),
        drop_params=config.get("drop_params"),
        extra_params=config.get("extra_params"),
    )


def test_defaults_use_max_completion_tokens_and_keep_temperature():
    params = _prepare(_provider())
    assert params[DEFAULT_MAX_TOKENS_PARAM] == 100  # max_completion_tokens
    assert "max_tokens" not in params
    assert params["temperature"] == 0.7


def test_legacy_deployment_can_request_max_tokens():
    params = _prepare(_provider(max_tokens_param="max_tokens"))
    assert params["max_tokens"] == 100
    assert "max_completion_tokens" not in params


def test_drop_params_strips_temperature_for_reasoning_deployment():
    params = _prepare(_provider(drop_params=["temperature"]))
    assert "temperature" not in params
    # ...even when temperature is supplied via kwargs.
    params = _prepare(
        _provider(drop_params=["temperature"]), kwargs={"temperature": 1}
    )
    assert "temperature" not in params


def test_extra_params_merged_with_caller_kwargs_taking_precedence():
    prov = _provider(extra_params={"reasoning_effort": "medium"})
    # config default applies when caller doesn't specify
    assert _prepare(prov)["reasoning_effort"] == "medium"
    # caller kwargs win over config defaults
    assert (
        _prepare(prov, kwargs={"reasoning_effort": "high"})["reasoning_effort"]
        == "high"
    )


def test_base_openai_provider_behaviour_unchanged():
    # The shared hook must not change the base provider: still max_tokens,
    # temperature always present, no Azure-only keys.
    base = OpenAIProvider(api_key="k")
    params = base._prepare_params(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.7,
        max_tokens=100,
        tools=None,
        kwargs={},
    )
    assert params["max_tokens"] == 100
    assert params["temperature"] == 0.7
    assert "max_completion_tokens" not in params
