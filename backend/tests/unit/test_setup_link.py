"""Setup links (`<console>/ui/?token=...`): minting (service + CLI), token
rules, the exchange/complete flow per AUTH_MODE, generic refusals, rate
limiting, and that no token ever reaches a log."""

import hashlib
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    consume_password_reset_token,
    create_password_reset_token,
    hash_password,
    verify_password,
)
from app.core.config import settings
from app.models.user import PasswordResetToken, User
from app.services import setup_link as setup_link_service
from app.services.setup_link import SetupLinkError, mint_setup_link

EXCHANGE = "/auth/setup-link/exchange"
COMPLETE = "/auth/setup-link/complete"


@pytest.fixture(autouse=True)
def _password_mode(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "password")
    monkeypatch.setattr(settings, "domain", "sinas.example.test")
    monkeypatch.setattr(settings, "console_url", None)


@pytest_asyncio.fixture
async def fresh_user(db: AsyncSession) -> User:
    """A user with no password yet (a fresh superadmin)."""
    user = User(email=f"first-{uuid.uuid4().hex[:8]}@example.com")
    db.add(user)
    await db.flush()
    return user


def _token_from(url: str) -> str:
    return url.split("?token=", 1)[1]


async def _tokens(db: AsyncSession, user: User) -> list[PasswordResetToken]:
    return list(
        (await db.execute(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id))).scalars()
    )


class TestMinting:
    async def test_url_points_at_the_console_root(self, db: AsyncSession, fresh_user: User):
        url, record, user = await mint_setup_link(db, fresh_user.email)
        assert url.startswith("https://sinas.example.test/ui/?token=")
        assert user.id == fresh_user.id
        assert record.expires_at > datetime.now(UTC) + timedelta(hours=23)

    async def test_defaults_to_the_superadmin(self, db: AsyncSession, fresh_user: User, monkeypatch):
        monkeypatch.setattr(settings, "superadmin_email", fresh_user.email.upper())
        _, _, user = await mint_setup_link(db)
        assert user.id == fresh_user.id

    async def test_ttl_is_configurable(self, db: AsyncSession, fresh_user: User, monkeypatch):
        monkeypatch.setattr(settings, "password_reset_token_ttl_hours", 2)
        _, record, _ = await mint_setup_link(db, fresh_user.email)
        assert record.expires_at < datetime.now(UTC) + timedelta(hours=2, minutes=1)

    @pytest.mark.parametrize("email", ["nobody@example.com", None])
    async def test_refuses_unknown_or_missing_user(self, db: AsyncSession, email, monkeypatch):
        monkeypatch.setattr(settings, "superadmin_email", None)
        with pytest.raises(SetupLinkError):
            await mint_setup_link(db, email)

    async def test_refuses_deactivated_user(self, db: AsyncSession, fresh_user: User):
        fresh_user.is_active = False
        await db.flush()
        with pytest.raises(SetupLinkError):
            await mint_setup_link(db, fresh_user.email)

    async def test_refuses_in_otp_only_mode(self, db: AsyncSession, fresh_user: User, monkeypatch):
        monkeypatch.setattr(settings, "auth_mode", "otp")
        with pytest.raises(SetupLinkError, match="AUTH_MODE=otp"):
            await mint_setup_link(db, fresh_user.email)
        assert await _tokens(db, fresh_user) == []


class TestCli:
    @pytest.fixture
    def _cli_db(self, db: AsyncSession, monkeypatch):
        """Point the CLI's session factory and engine at the test session."""
        import app.core.database as database

        @asynccontextmanager
        async def _session():
            yield db

        class _Engine:
            async def dispose(self):
                pass

        monkeypatch.setattr(database, "AsyncSessionLocal", _session)
        monkeypatch.setattr(database, "async_engine", _Engine())

    async def test_prints_only_the_url_on_stdout(self, db, fresh_user: User, capsys, _cli_db):
        from app.cli import _setup_link

        assert await _setup_link(fresh_user.email) == 0
        out, err = capsys.readouterr()
        url = out.strip()
        assert url.startswith("https://sinas.example.test/ui/?token=") and "\n" not in url
        assert fresh_user.email in err and "Single use" in err
        assert _token_from(url) not in err
        # The printed link is a real one
        assert await consume_password_reset_token(db, _token_from(url)) is not None

    async def test_failure_is_a_message_and_exit_1(self, fresh_user: User, capsys, _cli_db):
        from app.cli import _setup_link

        assert await _setup_link("nobody@example.com") == 1
        out, err = capsys.readouterr()
        assert out == "" and "No active user" in err

    def test_argument_parsing(self, monkeypatch):
        import app.cli as cli

        seen = {}

        async def fake(email):
            seen["email"] = email
            return 0

        monkeypatch.setattr(cli, "_setup_link", fake)
        assert cli.main(["setup-link", "--email", "a@example.com"]) == 0
        assert seen["email"] == "a@example.com"
        assert cli.main(["setup-link"]) == 0
        assert seen["email"] is None


class TestTokenRules:
    async def test_stored_hashed_only(self, db: AsyncSession, fresh_user: User):
        url, record, _ = await mint_setup_link(db, fresh_user.email)
        plain = _token_from(url)
        assert len(plain) >= 43  # 256 bits, urlsafe base64
        assert record.token_hash == hashlib.sha256(plain.encode()).hexdigest()
        assert plain not in {record.token_hash, str(record.id)}

    async def test_single_use(self, db: AsyncSession, fresh_user: User):
        plain, _ = await create_password_reset_token(db, str(fresh_user.id))
        assert await consume_password_reset_token(db, plain) is not None
        assert await consume_password_reset_token(db, plain) is None

    async def test_expires(self, db: AsyncSession, fresh_user: User):
        plain, record = await create_password_reset_token(db, str(fresh_user.id))
        await db.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.id == record.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        assert await consume_password_reset_token(db, plain) is None

    async def test_newer_link_invalidates_older(self, db: AsyncSession, fresh_user: User):
        old_url, _, _ = await mint_setup_link(db, fresh_user.email)
        new_url, _, _ = await mint_setup_link(db, fresh_user.email)
        assert await consume_password_reset_token(db, _token_from(old_url)) is None
        assert await consume_password_reset_token(db, _token_from(new_url)) is not None


class TestNoPasswordYet:
    async def test_exchange_then_set_password_signs_in(self, db, client, fresh_user: User):
        url, _, _ = await mint_setup_link(db, fresh_user.email)
        link = _token_from(url)

        r = await client.post(EXCHANGE, json={"token": link})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["email"] == fresh_user.email
        assert body["has_password"] is False
        assert "access_token" not in body  # exchanging never signs in
        setup_token = body["setup_token"]
        assert setup_token != link
        expires = datetime.fromisoformat(body["expires_at"])
        assert expires <= datetime.now(UTC) + setup_link_service.SETUP_SESSION_TTL

        # The link is spent on exchange: a copy from history is worthless
        r = await client.post(EXCHANGE, json={"token": link})
        assert r.status_code == 400

        r = await client.post(COMPLETE, json={"setup_token": setup_token, "new_password": "first-password"})
        assert r.status_code == 200, r.text
        signed_in = r.json()
        assert signed_in["access_token"] and signed_in["refresh_token"]
        assert signed_in["user"]["email"] == fresh_user.email

        await db.refresh(fresh_user)
        assert verify_password("first-password", fresh_user.password_hash)
        # Setting the password consumed the setup session and left nothing open
        assert all(t.used_at is not None for t in await _tokens(db, fresh_user))
        r = await client.post(COMPLETE, json={"setup_token": setup_token, "new_password": "second-try-pw"})
        assert r.status_code == 400

        # And the account now signs in normally
        r = await client.post("/auth/login", json={"email": fresh_user.email, "password": "first-password"})
        assert r.status_code == 200 and r.json()["access_token"]

    async def test_password_policy_enforced(self, db, client, fresh_user: User):
        url, _, _ = await mint_setup_link(db, fresh_user.email)
        setup_token = (await client.post(EXCHANGE, json={"token": _token_from(url)})).json()["setup_token"]
        for bad in ["short", "x" * 73]:
            r = await client.post(COMPLETE, json={"setup_token": setup_token, "new_password": bad})
            assert r.status_code == 422, bad
        # A rejected password doesn't spend the setup session
        r = await client.post(COMPLETE, json={"setup_token": setup_token, "new_password": "long-enough"})
        assert r.status_code == 200

    async def test_password_plus_otp_still_requires_the_code(self, db, client, fresh_user: User, monkeypatch):
        monkeypatch.setattr(settings, "auth_mode", "password+otp")
        sent = []

        async def fake_send(db, email, code):
            sent.append(email)

        monkeypatch.setattr("app.core.auth.send_otp_email_async", fake_send)
        url, _, _ = await mint_setup_link(db, fresh_user.email)
        setup_token = (await client.post(EXCHANGE, json={"token": _token_from(url)})).json()["setup_token"]

        r = await client.post(COMPLETE, json={"setup_token": setup_token, "new_password": "first-password"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["session_id"] and not body.get("access_token")
        assert sent == [fresh_user.email]
        await db.refresh(fresh_user)
        assert verify_password("first-password", fresh_user.password_hash)


class TestHasPassword:
    async def test_link_never_signs_in_but_can_replace_the_password(self, db, client, fresh_user: User):
        """The chosen behaviour for an account with a password: the exchange
        says so (the console offers "sign in normally"); it can only be used
        to choose a new password, like an admin reset link always could."""
        fresh_user.password_hash = hash_password("old-password")
        await db.flush()
        url, _, _ = await mint_setup_link(db, fresh_user.email)

        r = await client.post(EXCHANGE, json={"token": _token_from(url)})
        assert r.status_code == 200
        body = r.json()
        assert body["has_password"] is True
        assert "access_token" not in body and "refresh_token" not in body
        await db.refresh(fresh_user)
        assert verify_password("old-password", fresh_user.password_hash)

        r = await client.post(COMPLETE, json={"setup_token": body["setup_token"], "new_password": "new-password"})
        assert r.status_code == 200
        await db.refresh(fresh_user)
        assert verify_password("new-password", fresh_user.password_hash)


class TestRefusals:
    async def _bodies(self, client, tokens: list[str]) -> list[tuple[int, dict]]:
        out = []
        for t in tokens:
            r = await client.post(EXCHANGE, json={"token": t})
            out.append((r.status_code, r.json()))
        return out

    async def test_invalid_expired_used_superseded_inactive_look_the_same(self, db, client, fresh_user: User):
        used, _ = await create_password_reset_token(db, str(fresh_user.id))
        await consume_password_reset_token(db, used)

        expired, record = await create_password_reset_token(db, str(fresh_user.id))
        await db.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.id == record.id)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        superseded, _ = await create_password_reset_token(db, str(fresh_user.id))
        await create_password_reset_token(db, str(fresh_user.id))

        other = User(email=f"gone-{uuid.uuid4().hex[:8]}@example.com", is_active=False)
        db.add(other)
        await db.flush()
        inactive, _ = await create_password_reset_token(db, str(other.id))

        results = await self._bodies(client, ["not-a-token", used, expired, superseded, inactive])
        assert {status for status, _ in results} == {400}
        assert len({body["detail"] for _, body in results}) == 1
        detail = results[0][1]["detail"]
        assert fresh_user.email not in detail and other.email not in detail

    async def test_otp_only_mode_refuses_without_spending(self, db, client, fresh_user: User, monkeypatch):
        plain, _ = await create_password_reset_token(db, str(fresh_user.id))
        monkeypatch.setattr(settings, "auth_mode", "otp")
        r = await client.post(EXCHANGE, json={"token": plain})
        assert r.status_code == 400 and "code emailed" in r.json()["detail"]
        r = await client.post(COMPLETE, json={"setup_token": plain, "new_password": "whatever-pw"})
        assert r.status_code == 400
        await db.refresh(fresh_user)
        assert fresh_user.password_hash is None

    async def test_rate_limited(self, client, monkeypatch):
        monkeypatch.setattr(settings, "rate_limit_setup_link_ip_max", 3)
        codes = [(await client.post(EXCHANGE, json={"token": f"guess-{i}"})).status_code for i in range(5)]
        assert codes[:3] == [400, 400, 400]
        assert codes[3:] == [429, 429]
        codes = [
            (await client.post(COMPLETE, json={"setup_token": f"g-{i}", "new_password": "whatever-pw"})).status_code
            for i in range(5)
        ]
        assert codes[3:] == [429, 429]


class TestNothingLogsTheToken:
    async def test_mint_exchange_complete_logs_ids_not_tokens(self, db, client, fresh_user: User, caplog):
        with caplog.at_level(logging.DEBUG):
            url, _, _ = await mint_setup_link(db, fresh_user.email)
            link = _token_from(url)
            setup_token = (await client.post(EXCHANGE, json={"token": link})).json()["setup_token"]
            await client.post(COMPLETE, json={"setup_token": setup_token, "new_password": "first-password"})
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert link not in text and setup_token not in text and "first-password" not in text
        assert f"Setup link opened for user {fresh_user.id}" in text
        assert f"Password set via setup link for user {fresh_user.id}" in text


class TestRequestLogRedaction:
    def test_query_params(self):
        from app.middleware.request_logger import redact_query_params

        assert redact_query_params("token=abc&page=2") == {"token": "***REDACTED***", "page": "2"}
        assert redact_query_params("TOKEN=abc")["TOKEN"] == "***REDACTED***"
        assert redact_query_params("") == {}

    def test_urls_and_bodies(self):
        from app.middleware.request_logger import redact_body, redact_url_query

        assert redact_url_query("/ui/?token=abc") == "/ui/?token=***REDACTED***"
        assert redact_url_query("https://h/ui?x=1&token=abc#f") == "https://h/ui?x=1&token=***REDACTED***#f"
        assert redact_url_query("/agents?page=2") == "/agents?page=2"
        body = redact_body({"setup_token": "s", "reset_token": "r", "new_password": "p", "token": "t"})
        assert set(body.values()) == {"***REDACTED***"}

    def test_uvicorn_access_log_filter(self):
        from app.middleware.request_logger import RedactQueryTokensFilter

        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:5000", "GET", "/ui/?token=abc", "1.1", 200), None,
        )
        assert RedactQueryTokensFilter().filter(record)
        assert "abc" not in record.getMessage() and "token=***REDACTED***" in record.getMessage()

    # Names the browser (URLSearchParams) reads as `token`: percent-encoded,
    # partly encoded, any case. Review round: these slipped past.
    ENCODED_NAMES = ["tok%65n", "%74oken", "%74%6F%6B%65%6E", "TOKEN", "ToKeN", "TOK%45N", "setup%5Ftoken", "api%5fkey"]

    @pytest.mark.parametrize("name", ENCODED_NAMES)
    def test_encoded_and_mixed_case_names_in_query_params(self, name):
        from app.middleware.request_logger import redact_query_params

        assert redact_query_params(f"page=2&{name}=s3cret") == {"page": "2", name: "***REDACTED***"}

    @pytest.mark.parametrize("name", ENCODED_NAMES)
    def test_encoded_and_mixed_case_names_in_urls(self, name):
        from app.middleware.request_logger import redact_url_query

        for url in (f"/ui/?{name}=s3cret", f"https://h:51245/ui?x=1&{name}=s3cret#f"):
            assert "s3cret" not in redact_url_query(url), url

    @pytest.mark.parametrize("name", ENCODED_NAMES)
    def test_encoded_and_mixed_case_names_in_the_access_log(self, name):
        from app.middleware.request_logger import RedactQueryTokensFilter

        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:5000", "GET", f"/ui/?{name}=s3cret", "1.1", 200), None,
        )
        RedactQueryTokensFilter().filter(record)
        assert "s3cret" not in record.getMessage()

    def test_the_names_redacted_are_the_ones_the_browser_reads(self):
        """Cross-check against the decoding URLSearchParams does (urllib's
        parse_qsl decodes names the same way: percent-escapes, `+`)."""
        from urllib.parse import parse_qsl

        for name in self.ENCODED_NAMES:
            decoded = parse_qsl(f"{name}=v")[0][0]
            assert decoded.lower() in {"token", "setup_token", "api_key"}

    def test_non_credentials_untouched_and_nested_urls_masked(self):
        from app.middleware.request_logger import redact_query_params, redact_url_query

        assert redact_url_query("/agents?page=2&tokens=5&q=a%20b") == "/agents?page=2&tokens=5&q=a%20b"
        assert redact_url_query("/login?next=/ui/?token=s3cret") == "/login?next=/ui/?token=***REDACTED***"
        assert redact_query_params("next=/ui/?tok%65n=s3cret") == {"next": "/ui/?tok%65n=***REDACTED***"}


def test_deeply_nested_urls_are_masked_without_recursing_forever():
    """A few hundred nested `?next=` values must not stop the request with a
    RecursionError; past the nesting cap the rest is masked whole."""
    from app.middleware.request_logger import redact_query_params, redact_url_query

    deep = "/ui/?token=s3cr3t"
    for _ in range(500):
        deep = f"/a?next={deep}"
    out = redact_url_query(deep)
    assert "s3cr3t" not in out
    assert "s3cr3t" not in str(redact_query_params(deep.split("?", 1)[1]))

    shallow = "/a?next=/b?next=/ui/?tok%65n=s3cr3t&x=1"
    masked = redact_url_query(shallow)
    assert "s3cr3t" not in masked and masked.endswith("&x=1")
    assert "/b?next=/ui/?tok%65n=***REDACTED***" in masked
