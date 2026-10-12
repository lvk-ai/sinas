"""LLM calls reuse the SDK client, and with it warm connections.

A provider is built for every LLM call, and each built its own SDK client,
so every message and tool round paid a new TCP and TLS handshake to the
model API before the first token.
"""
import asyncio

from app.providers import AnthropicProvider, OpenAIProvider
from app.providers.azure_openai_provider import AzureOpenAIProvider
from app.providers.clients import close_shared_clients, shared_client
from app.providers.mistral_provider import MistralProvider
from app.providers.ollama_provider import OllamaProvider


async def test_the_same_settings_share_a_client():
    a, b = OpenAIProvider(api_key="k1", base_url="http://x/v1"), OpenAIProvider(api_key="k1", base_url="http://x/v1")
    assert a.client is b.client
    assert AnthropicProvider(api_key="k1").client is AnthropicProvider(api_key="k1").client
    assert MistralProvider(api_key="k1").client is MistralProvider(api_key="k1").client
    azure = dict(api_key="k1", azure_endpoint="https://a.openai.azure.com", azure_deployment="d")
    assert AzureOpenAIProvider(**azure).client is AzureOpenAIProvider(**azure).client


async def test_other_settings_get_their_own():
    base = OpenAIProvider(api_key="k1", base_url="http://x/v1").client
    assert OpenAIProvider(api_key="k2", base_url="http://x/v1").client is not base
    assert OpenAIProvider(api_key="k1", base_url="http://y/v1").client is not base
    azure = dict(api_key="k1", azure_endpoint="https://a.openai.azure.com")
    assert (
        AzureOpenAIProvider(**azure, azure_deployment="d1").client
        is not AzureOpenAIProvider(**azure, azure_deployment="d2").client
    )


async def test_per_agent_settings_stay_per_provider():
    a, b = AnthropicProvider(api_key="k1"), AnthropicProvider(api_key="k1")
    a.effort = "low"
    assert b.effort is None and a.client is b.client


def test_each_event_loop_has_its_own():
    async def make():
        return OpenAIProvider(api_key="k1").client

    assert asyncio.run(make()) is not asyncio.run(make())


async def test_ollama_keeps_its_client_open_between_calls():
    provider = OllamaProvider(base_url="http://ollama")
    async with provider._http() as first:
        pass
    assert not first.is_closed
    async with OllamaProvider(base_url="http://ollama")._http() as second:
        assert second is first


async def test_a_closed_client_is_replaced_and_shutdown_closes_them():
    first = OpenAIProvider(api_key="k1").client
    await first.close()
    second = OpenAIProvider(api_key="k1").client
    assert second is not first
    await close_shared_clients()
    assert second.is_closed()
    assert shared_client(("x",), object) is not None
