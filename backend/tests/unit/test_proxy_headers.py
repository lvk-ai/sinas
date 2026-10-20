"""The client IP behind a reverse proxy (#78).

Every client behind Caddy or an ingress used to share one IP rate-limit
bucket: uvicorn ran without trusted proxies, so the client was always the
proxy. uvicorn's --proxy-headers rewrites scope["client"] from
X-Forwarded-For, but only on connections from FORWARDED_ALLOW_IPS, walking
the header right to left to the first untrusted address. The rate limiter
and the request logger both read scope["client"].

Pinned here: every launch passes the flag, each deployment's default trust is
what it claims, a proxied request carries its real client, and a client
connecting directly can't choose its own.
"""

import ipaddress
import re
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core import rate_limit
from app.middleware.request_logger import RequestLoggerMiddleware

REPO = Path(__file__).resolve().parents[3]

requires_repo = pytest.mark.skipif(
    not (REPO / "docker-compose.yml").is_file(), reason="deployment files not in this checkout"
)


def _default(value: str) -> str:
    """The default of a compose `${VAR:-default}`."""
    match = re.fullmatch(r"\$\{\w+:-([^}]*)\}", value)
    return match.group(1) if match else value


def _compose() -> dict:
    return yaml.safe_load((REPO / "docker-compose.yml").read_text())


def _helm_default() -> str:
    values = yaml.safe_load((REPO / "charts/sinas/values.yaml").read_text())
    return ",".join(values["backend"]["forwardedAllowIps"])


def _lite_default() -> str:
    lite = yaml.safe_load((REPO / "docker-compose.lite.yml").read_text())
    return _default(lite["services"]["sinas"]["environment"]["FORWARDED_ALLOW_IPS"])


async def _client_seen(trusted: str, peer: str, forwarded_for: str | None = None) -> str:
    seen: dict[str, str] = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"][0]
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    transport = ASGITransport(app=ProxyHeadersMiddleware(app, trusted_hosts=trusted), client=(peer, 40000))
    async with AsyncClient(transport=transport, base_url="http://sinas.test") as client:
        await client.get("/", headers={"X-Forwarded-For": forwarded_for} if forwarded_for else {})
    return seen["client"]


@requires_repo
def test_every_uvicorn_launch_trusts_proxy_headers():
    launchers = [
        *REPO.glob("backend/Dockerfile*"),
        *REPO.glob("docker-compose*.yml"),
        *REPO.glob("charts/sinas/templates/*.yaml"),
        REPO / "tests/e2e/oidc/run.sh",
    ]
    launches = [
        (path.relative_to(REPO), line.strip())
        for path in launchers
        for line in path.read_text().splitlines()
        if "uvicorn app.main:app" in line
    ]
    # The image CMDs, compose (full + dev), the chart, the e2e harness.
    assert len(launches) >= 6, launches
    assert [launch for launch in launches if "--proxy-headers" not in launch[1]] == []


@requires_repo
def test_compose_trusts_caddy_alone():
    compose = _compose()
    services = compose["services"]
    caddy_ip = _default(services["caddy"]["networks"]["proxy"]["ipv4_address"])
    assert _default(services["backend"]["environment"]["FORWARDED_ALLOW_IPS"]) == caddy_ip
    # Caddy's connections to the backend come from that address only.
    assert list(services["caddy"]["networks"]) == ["proxy"]
    ipam = compose["networks"]["proxy"]["ipam"]["config"][0]
    address = ipaddress.ip_address(caddy_ip)
    assert address in ipaddress.ip_network(_default(ipam["subnet"]))
    # Dynamic addresses (and the gateway Docker takes from them) never Caddy's.
    assert address not in ipaddress.ip_network(_default(ipam["ip_range"]))
    # Code-execution containers join the backend's first network (auto).
    assert services["backend"]["networks"][0] == "default"


@requires_repo
async def test_compose_behind_caddy():
    trusted = _default(_compose()["services"]["backend"]["environment"]["FORWARDED_ALLOW_IPS"])
    # Caddy replaces any client-sent X-Forwarded-For with the peer it saw.
    assert await _client_seen(trusted, trusted, "203.0.113.7") == "203.0.113.7"
    # Straight to :8000, from outside or via the bridge gateway: ignored.
    assert await _client_seen(trusted, "198.51.100.9", "203.0.113.7") == "198.51.100.9"
    assert await _client_seen(trusted, "10.254.253.128", "203.0.113.7") == "10.254.253.128"


@requires_repo
async def test_helm_behind_an_ingress():
    trusted = _helm_default()
    # The ingress controller (a pod IP) appends the peer it saw; whatever the
    # client sent before that is not believed.
    assert await _client_seen(trusted, "10.42.1.17", "6.6.6.6, 203.0.113.7") == "203.0.113.7"
    assert await _client_seen(trusted, "100.64.3.2", "203.0.113.8") == "203.0.113.8"
    assert await _client_seen(trusted, "fd00::12", "2001:db8::5") == "2001:db8::5"
    # A public peer is never a trusted proxy.
    assert await _client_seen(trusted, "198.51.100.9", "203.0.113.7") == "198.51.100.9"


@requires_repo
async def test_lite_trusts_no_proxy_by_default():
    assert await _client_seen(_lite_default(), "172.18.0.1", "203.0.113.7") == "172.18.0.1"


async def test_rate_limiter_and_request_log_see_the_forwarded_client(monkeypatch):
    limited: list[str] = []
    logged: list[str | None] = []

    async def fake_check(key, max_requests, window_seconds):
        limited.append(key)

    async def fake_log_request(**fields):
        logged.append(fields["ip_address"])

    monkeypatch.setattr(rate_limit, "check_rate_limit", fake_check)
    monkeypatch.setattr("app.middleware.request_logger.clickhouse_logger.log_request", fake_log_request)

    api = FastAPI()

    @api.post("/login")
    async def login(request: Request):
        await rate_limit.rate_limit_by_ip(request, "login")
        return {}

    # As uvicorn stacks them: proxy headers outermost, then the app's own.
    app = ProxyHeadersMiddleware(RequestLoggerMiddleware(api), trusted_hosts="10.254.253.2")
    for peer, forwarded_for in (("10.254.253.2", "203.0.113.7"), ("198.51.100.9", "203.0.113.7")):
        transport = ASGITransport(app=app, client=(peer, 40000))
        async with AsyncClient(transport=transport, base_url="http://sinas.test") as client:
            assert (await client.post("/login", headers={"X-Forwarded-For": forwarded_for})).status_code == 200

    assert limited == ["login:ip:203.0.113.7", "login:ip:198.51.100.9"]
    assert logged == ["203.0.113.7", "198.51.100.9"]
