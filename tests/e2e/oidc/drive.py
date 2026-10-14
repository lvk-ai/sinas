"""Drive SSO login against a real Keycloak, as a browser would (run.sh starts
everything and passes the settings in the environment).

Python standard library only. Secrets come from the environment and are
never printed.
"""
import base64
import hashlib
import html
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

SINAS = os.environ["SINAS_URL"]  # http://localhost:18000
KC = os.environ["KC_URL"]  # http://localhost:18080
REALM = os.environ.get("KC_REALM", "sinas-e2e")
PROVIDER = "keycloak"
USER_EMAIL = "ann@acme.example.com"
CONSOLE_CALLBACK = f"{SINAS}/ui/sso/callback"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class LocalhostIsSecure(http.cookiejar.DefaultCookiePolicy):
    """Browsers treat http://localhost as a secure context and send Secure
    cookies there (Keycloak marks its session cookies Secure even over http)."""

    def return_ok_secure(self, cookie, request):
        return not cookie.secure or urllib.parse.urlsplit(request.get_full_url()).hostname == "localhost"


class Browser:
    """Cookies, no automatic redirects (we follow them, to see where they go)."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar(policy=LocalhostIsSecure())
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect())

    def request(self, method, url, data=None, headers=None, json_body=None):
        body = None
        headers = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif data is not None:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.status, resp.headers, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()

    def follow(self, url, method="GET", data=None):
        """Follow redirects until the console's SSO route or a page."""
        for _ in range(10):
            status, headers, body = self.request(method, url, data=data)
            if status in (301, 302, 303, 307) and headers.get("Location"):
                url = urllib.parse.urljoin(url, headers["Location"])
                method, data = "GET", None
                if url.startswith(CONSOLE_CALLBACK):
                    return url, None
                continue
            return url, body
        raise RuntimeError("too many redirects")


def api(browser, method, path, token=None, body=None, expect=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    status, _, text = browser.request(method, f"{SINAS}{path}", headers=headers, json_body=body)
    if expect is not None and status != expect:
        raise AssertionError(f"{method} {path}: expected {expect}, got {status}: {text[:300]}")
    return status, (json.loads(text) if text else None)


def pkce():
    verifier = base64.urlsafe_b64encode(os.urandom(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def sso_login(browser, password, *, return_to="/agents"):
    """Start at Sinas, sign in at Keycloak (or ride its session), come back,
    exchange the one-time code. Returns (exchange body, whether a login form was shown)."""
    verifier, challenge = pkce()
    start = f"{SINAS}/auth/oidc/{PROVIDER}/start?" + urllib.parse.urlencode(
        {"code_challenge": challenge, "return_to": return_to}
    )
    url, page = browser.follow(start)
    form_shown = False
    if page is not None:
        match = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', page)
        if not match:
            raise AssertionError(f"no Keycloak login form at {url.split('?')[0]}")
        form_shown = True
        url, page = browser.follow(
            html.unescape(match.group(1)), method="POST",
            data={"username": "ann", "password": password, "credentialId": ""},
        )
        if page is not None:
            raise AssertionError(
                f"Keycloak didn't redirect back after login (at {url.split('?')[0]}): {_page_message(page)}"
            )
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    if "error" in params:
        raise AssertionError(f"SSO refused: {params['error']}")
    assert set(params) == {"code"}, f"unexpected console parameters: {sorted(params)}"
    status, body = api(browser, "POST", "/auth/oidc/exchange",
                       body={"code": params["code"], "code_verifier": verifier}, expect=200)
    # Single use.
    again, _ = api(browser, "POST", "/auth/oidc/exchange", body={"code": params["code"], "code_verifier": verifier})
    assert again == 400, f"one-time code reused: {again}"
    return body, form_shown


def _page_message(page):
    """What a Keycloak page says (its title and any error), for a failure."""
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    alert = re.search(r'class="[^"]*(?:alert|kc-feedback-text|instruction)[^"]*"[^>]*>(.*?)<', page, re.S)
    parts = [m.group(1).strip() for m in (title, alert) if m]
    return html.unescape(" | ".join(parts)) or "(no message)"


class KeycloakAdmin:
    def __init__(self, browser):
        self.browser = browser
        status, _, text = browser.request("POST", f"{KC}/realms/master/protocol/openid-connect/token", data={
            "grant_type": "password", "client_id": "admin-cli",
            "username": os.environ["KC_ADMIN_USER"], "password": os.environ["KC_ADMIN_PASSWORD"],
        })
        assert status == 200, f"Keycloak admin token: {status}"
        self.token = json.loads(text)["access_token"]

    def call(self, method, path):
        status, _, text = self.browser.request(
            method, f"{KC}/admin/realms/{REALM}{path}", headers={"Authorization": f"Bearer {self.token}"}
        )
        assert status in (200, 204), f"Keycloak {method} {path}: {status}"
        return json.loads(text) if text else None

    def remove_from_group(self, username, group):
        user = self.call("GET", f"/users?username={username}&exact=true")[0]
        group_id = next(g["id"] for g in self.call("GET", "/groups") if g["name"] == group)
        self.call("DELETE", f"/users/{user['id']}/groups/{group_id}")


def main():
    results = {}
    admin = Browser()  # the operator, on the API
    _, login = api(admin, "POST", "/auth/login", body={
        "email": os.environ["SUPERADMIN_EMAIL"], "password": os.environ["SUPERADMIN_PASSWORD"],
    }, expect=200)
    token = login["access_token"]

    for role in ("e2e-admins", "e2e-readers", "e2e-manual"):
        api(admin, "POST", "/api/v1/roles", token, {"name": role}, expect=201)
    api(admin, "POST", "/api/v1/secrets", token, {
        "name": "KEYCLOAK_CLIENT_SECRET", "value": os.environ["KC_CLIENT_SECRET"], "visibility": "shared",
    }, expect=201)
    _, provider = api(admin, "POST", "/api/v1/identity-providers", token, {
        "name": PROVIDER, "display_name": "Keycloak", "issuer": f"{KC}/realms/{REALM}",
        "client_id": "sinas-console", "client_secret": "KEYCLOAK_CLIENT_SECRET",
        "auto_provision": True, "allowed_domains": ["acme.example.com"], "session_hours": 2,
        "role_mappings": [
            {"group": "sinas-admins", "roles": ["e2e-admins"]},
            {"group": "sinas-readers", "roles": ["e2e-readers"]},
        ],
    }, expect=201)
    results["redirect_uri"] = provider["redirect_uri"]
    _, listed = api(admin, "GET", "/auth/oidc/providers", expect=200)
    assert listed == [{"name": PROVIDER, "display_name": "Keycloak"}], listed

    # 1. First login: provisioned, both groups mapped.
    browser = Browser()
    body, form = sso_login(browser, os.environ["KC_USER_PASSWORD"])
    assert form, "expected Keycloak's login form on the first sign-in"
    assert body["user"]["email"] == USER_EMAIL, body["user"]["email"]
    assert set(body["user"]["roles"]) == {"e2e-admins", "e2e-readers"}, body["user"]["roles"]
    assert body["return_to"] == "/agents"
    results["first_login_roles"] = sorted(body["user"]["roles"])
    user_id = body["user"]["id"]
    _, me = api(browser, "GET", "/auth/me", body["access_token"], expect=200)
    assert me["id"] == user_id

    # An admin grants a role by hand; Keycloak drops the user from sinas-admins.
    api(admin, "POST", "/api/v1/roles/e2e-manual/members", token, {"user_id": user_id}, expect=200)
    KeycloakAdmin(Browser()).remove_from_group("ann", "sinas-admins")

    # 2. Second login (Keycloak's session: no form): the role goes, the rest stays.
    body, form = sso_login(browser, os.environ["KC_USER_PASSWORD"])
    assert body["user"]["id"] == user_id
    assert set(body["user"]["roles"]) == {"e2e-readers", "e2e-manual"}, body["user"]["roles"]
    results["second_login_roles"] = sorted(body["user"]["roles"])
    results["second_login_reused_idp_session"] = not form
    _, admins = api(admin, "GET", "/api/v1/roles/e2e-admins/members", token, expect=200)
    assert all(m["user_id"] != user_id for m in admins)
    _, readers = api(admin, "GET", "/api/v1/roles/e2e-readers/members", token, expect=200)
    _, manual = api(admin, "GET", "/api/v1/roles/e2e-manual/members", token, expect=200)
    sources = {
        "e2e-readers": next(m["source"] for m in readers if m["user_id"] == user_id),
        "e2e-manual": next(m["source"] for m in manual if m["user_id"] == user_id),
    }
    assert sources == {"e2e-readers": f"idp:{PROVIDER}", "e2e-manual": None}, sources
    results["membership_sources"] = sources

    # The SSO session refreshes.
    api(browser, "POST", "/auth/refresh", body={"refresh_token": body["refresh_token"]}, expect=200)

    # A forged state is refused, back at the console with a message.
    status, headers, _ = browser.request("GET", f"{SINAS}/auth/oidc/{PROVIDER}/callback?code=x&state=forged")
    assert status == 302 and "error=" in headers["Location"], (status, headers.get("Location"))

    # 3. Enforcement: acme.example.com must use SSO; the superadmin still signs in.
    api(admin, "PUT", f"/api/v1/identity-providers/{PROVIDER}", token, {"enforce_for_domains": ["acme.example.com"]}, expect=200)
    status, refused = api(Browser(), "POST", "/auth/login", body={"email": USER_EMAIL, "password": "irrelevant"})
    assert status == 403 and "Keycloak" in refused["detail"], (status, refused)
    _, found = api(Browser(), "POST", "/auth/oidc/discover", body={"email": USER_EMAIL}, expect=200)
    assert found["provider"]["name"] == PROVIDER
    api(Browser(), "POST", "/auth/login", body={
        "email": os.environ["SUPERADMIN_EMAIL"], "password": os.environ["SUPERADMIN_PASSWORD"],
    }, expect=200)
    api(browser, "POST", "/auth/refresh", body={"refresh_token": body["refresh_token"]}, expect=200)
    results["enforcement"] = "password login refused for acme.example.com, superadmin and SSO session fine"

    # 4. Change history recorded the provider's create and update.
    _, history = api(admin, "GET", "/api/v1/config/history?kind=identityProviders", token, expect=200)
    results["history"] = [r["action"] for r in history if r["resource_key"] == PROVIDER]
    assert sorted(results["history"]) == ["create", "update"], results["history"]

    print(json.dumps(results, indent=2))
    print("E2E OK")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"E2E FAILED: {e}")
        sys.exit(1)
