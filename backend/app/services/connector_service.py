"""Connector service — executes HTTP operations in-process."""
import asyncio
import base64
import logging
import re
import time
from typing import Any, Optional

import httpx
from jinja2 import Template
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encryption_service
from app.models.connector import Connector
from app.models.secret import Secret

logger = logging.getLogger(__name__)

# Connection pool limits
MAX_CONNECTIONS = 200          # Total across all hosts
MAX_CONNECTIONS_PER_HOST = 20  # Per individual host
MAX_CONCURRENT_REQUESTS = 100  # Semaphore limit

# OAuth 2.0 client-credentials token caching
OAUTH_TOKEN_TTL_SKEW = 60      # Refresh this many seconds before the token actually expires
OAUTH_DEFAULT_TTL = 3600       # Assumed lifetime when the token response omits expires_in


class ConnectorService:
    """Executes connector operations in-process via httpx with connection pooling."""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        # OAuth client-credentials token cache: cache_key -> (access_token, expires_at_monotonic)
        self._oauth_tokens: dict[str, tuple[str, float]] = {}
        # Per-cache-key locks to avoid stampeding the token endpoint on concurrent misses
        self._oauth_locks: dict[str, asyncio.Lock] = {}

    def _get_client(self) -> httpx.AsyncClient:
        """Get or create the shared httpx client with connection pooling."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                limits=httpx.Limits(
                    max_connections=MAX_CONNECTIONS,
                    max_keepalive_connections=MAX_CONNECTIONS_PER_HOST,
                ),
                follow_redirects=True,
            )
        return self._client

    async def close(self):
        """Close the shared client. Called on shutdown."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def execute_operation(
        self,
        db: AsyncSession,
        connector: Connector,
        operation_name: str,
        parameters: dict[str, Any],
        user_token: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Execute a connector operation and return the response."""
        operation = connector.get_operation(operation_name)
        if not operation:
            raise ValueError(f"Operation '{operation_name}' not found on connector '{connector.namespace}/{connector.name}'")

        # Resolve auth (private secrets override shared for this user)
        auth_headers, auth_query = await self._resolve_auth(db, connector.auth, user_token, user_id)

        # Build request
        method = operation["method"]
        path = self._render_path(operation["path"], parameters)
        url = connector.base_url.rstrip("/") + path

        mapping = operation.get("request_body_mapping", "json")
        request_headers = {**connector.headers, **auth_headers}
        json_body = None
        query_params = None

        # Extract path params from Jinja2 template to exclude from body/query
        path_param_names = set(re.findall(r"\{\{\s*(\w+)\s*\}\}", operation["path"]))
        non_path_params = {k: v for k, v in parameters.items() if k not in path_param_names}

        if mapping == "json":
            json_body = non_path_params
        elif mapping == "query":
            query_params = non_path_params
        elif mapping == "path_and_json":
            json_body = non_path_params
        elif mapping == "path_and_query":
            query_params = non_path_params

        # Auth may contribute query params (e.g. an api_key with position="query")
        if auth_query:
            query_params = {**(query_params or {}), **auth_query}

        # Execute with retry, respecting concurrency limit
        retry_config = connector.retry or {}
        max_attempts = retry_config.get("max_attempts", 1)
        backoff = retry_config.get("backoff", "none")
        timeout = connector.timeout_seconds

        last_error = None
        for attempt in range(max_attempts):
            try:
                async with self._semaphore:
                    start = time.monotonic()
                    client = self._get_client()
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=request_headers,
                        json=json_body,
                        params=query_params,
                        timeout=float(timeout),
                    )
                    elapsed_ms = (time.monotonic() - start) * 1000

                # Parse response (outside semaphore — no need to hold it during parsing)
                response_mapping = operation.get("response_mapping", "json")
                if response_mapping == "json":
                    try:
                        body = response.json()
                    except Exception:
                        body = response.text
                else:
                    body = response.text

                result = {
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "body": body,
                    "elapsed_ms": round(elapsed_ms, 1),
                }
                logger.info(f"Connector {connector.namespace}/{connector.name}/{operation_name}: {response.status_code} in {elapsed_ms:.0f}ms")
                return result

            except httpx.TimeoutException as e:
                # Timeouts are not retried — the API is too slow, retrying won't help
                logger.error(f"Connector {connector.namespace}/{connector.name}/{operation_name} timed out after {timeout}s")
                raise
            except Exception as e:
                logger.error(f"Connector {connector.namespace}/{connector.name}/{operation_name} attempt {attempt+1} failed: {e}")
                last_error = e
                if attempt < max_attempts - 1:
                    delay = self._backoff_delay(attempt, backoff)
                    if delay > 0:
                        await asyncio.sleep(delay)
                else:
                    raise

        raise last_error  # Should not reach here

    async def _resolve_auth(
        self, db: AsyncSession, auth_config: dict[str, Any], user_token: Optional[str],
        user_id: Optional[str] = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Resolve auth config into (headers, query_params). Private secrets override shared.

        Most auth types contribute a header; an api_key with position="query" contributes
        a query parameter instead. The query dict is empty for all other types.
        """
        auth_type = auth_config.get("type", "none")

        if auth_type == "none":
            return {}, {}

        if auth_type == "sinas_token":
            if not user_token:
                logger.warning("sinas_token auth requested but no user token available")
                return {}, {}
            return {"Authorization": f"Bearer {user_token}"}, {}

        if auth_type == "oauth2_client_credentials":
            token = await self._get_client_credentials_token(db, auth_config, user_id)
            if not token:
                return {}, {}
            return {"Authorization": f"Bearer {token}"}, {}

        # All other types require a secret
        secret_name = auth_config.get("secret")
        if not secret_name:
            logger.warning(f"Auth type '{auth_type}' requires a secret but none configured")
            return {}, {}

        secret_value = await self._resolve_secret_value(db, secret_name, user_id)
        if secret_value is None:
            logger.warning(f"Secret '{secret_name}' not found for connector auth")
            return {}, {}

        if auth_type == "bearer":
            return {"Authorization": f"Bearer {secret_value}"}, {}
        elif auth_type == "basic":
            encoded = base64.b64encode(secret_value.encode()).decode()
            return {"Authorization": f"Basic {encoded}"}, {}
        elif auth_type == "api_key":
            if auth_config.get("position") == "query":
                param_name = auth_config.get("param_name") or "api_key"
                return {}, {param_name: secret_value}
            header_name = auth_config.get("header") or "X-Api-Key"
            return {header_name: secret_value}, {}

        return {}, {}

    async def _resolve_secret_value(
        self, db: AsyncSession, secret_name: str, user_id: Optional[str] = None
    ) -> Optional[str]:
        """Resolve a Secret by name to its decrypted value. Private overrides shared."""
        secret = None
        if user_id:
            result = await db.execute(
                select(Secret).where(
                    and_(Secret.name == secret_name, Secret.user_id == user_id, Secret.visibility == "private")
                )
            )
            secret = result.scalar_one_or_none()

        if not secret:
            result = await db.execute(
                select(Secret).where(
                    and_(Secret.name == secret_name, Secret.visibility == "shared")
                )
            )
            secret = result.scalar_one_or_none()

        if not secret:
            return None

        return encryption_service.decrypt(secret.encrypted_value)

    async def _get_client_credentials_token(
        self, db: AsyncSession, auth_config: dict[str, Any], user_id: Optional[str] = None
    ) -> Optional[str]:
        """Fetch (and cache) an OAuth 2.0 client-credentials access token.

        The client secret is resolved from the Secret named by `auth_config["secret"]`
        (private overrides shared, same as other auth types). Tokens are cached in-process
        keyed by (user, endpoint, client, scope) until shortly before they expire.
        """
        token_url = auth_config.get("token_url")
        client_id = auth_config.get("client_id")
        secret_name = auth_config.get("secret")
        if not token_url or not client_id or not secret_name:
            logger.warning(
                "oauth2_client_credentials auth requires token_url, client_id, and secret"
            )
            return None

        scopes = auth_config.get("scopes") or []
        scope_str = " ".join(scopes) if isinstance(scopes, list) else str(scopes)
        client_auth_method = auth_config.get("client_auth_method") or "body"

        # Distinct creds/scope/endpoint get distinct cache entries. user_id is included
        # because a private secret override yields a different (per-user) token.
        cache_key = f"{user_id or 'shared'}|{token_url}|{client_id}|{scope_str}"

        # Fast path: a still-valid cached token.
        cached = self._oauth_tokens.get(cache_key)
        if cached and cached[1] > time.monotonic():
            return cached[0]

        lock = self._oauth_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            # Re-check under the lock — another coroutine may have refreshed while we waited.
            cached = self._oauth_tokens.get(cache_key)
            if cached and cached[1] > time.monotonic():
                return cached[0]

            client_secret = await self._resolve_secret_value(db, secret_name, user_id)
            if client_secret is None:
                logger.warning(f"OAuth client secret '{secret_name}' not found")
                return None

            data = {"grant_type": "client_credentials"}
            if scope_str:
                data["scope"] = scope_str
            extra = auth_config.get("token_params")
            if isinstance(extra, dict):
                data.update({k: str(v) for k, v in extra.items()})

            headers = {"Accept": "application/json"}
            if client_auth_method == "basic":
                encoded = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
                headers["Authorization"] = f"Basic {encoded}"
            else:  # "body" — client_secret_post
                data["client_id"] = client_id
                data["client_secret"] = client_secret

            try:
                async with self._semaphore:
                    client = self._get_client()
                    resp = await client.post(token_url, data=data, headers=headers, timeout=30.0)
            except Exception as e:
                logger.error(f"OAuth token request to {token_url} failed: {e}")
                return None

            if resp.status_code != 200:
                logger.error(
                    f"OAuth token endpoint {token_url} returned {resp.status_code}: {resp.text[:200]}"
                )
                return None

            try:
                payload = resp.json()
            except Exception:
                logger.error(f"OAuth token endpoint {token_url} returned a non-JSON body")
                return None

            access_token = payload.get("access_token")
            if not access_token:
                logger.error(f"OAuth token endpoint {token_url} response missing access_token")
                return None

            expires_in = payload.get("expires_in")
            try:
                ttl = int(expires_in) if expires_in is not None else OAUTH_DEFAULT_TTL
            except (TypeError, ValueError):
                ttl = OAUTH_DEFAULT_TTL
            expires_at = time.monotonic() + max(ttl - OAUTH_TOKEN_TTL_SKEW, 1)
            self._oauth_tokens[cache_key] = (access_token, expires_at)
            logger.info(
                f"Obtained OAuth client-credentials token from {token_url} (ttl={ttl}s)"
            )
            return access_token

    def _render_path(self, path_template: str, parameters: dict[str, Any]) -> str:
        """Render Jinja2 path template with parameters."""
        if "{{" not in path_template:
            return path_template
        try:
            template = Template(path_template)
            return template.render(**parameters)
        except Exception:
            return path_template

    def _backoff_delay(self, attempt: int, strategy: str) -> float:
        """Calculate backoff delay in seconds."""
        if strategy == "exponential":
            return min(2 ** attempt * 0.5, 30.0)
        elif strategy == "linear":
            return min((attempt + 1) * 1.0, 30.0)
        return 0.0


connector_service = ConnectorService()
