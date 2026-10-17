"""Superadmin password bootstrap (initialize_superadmin).

Two modes when AUTH_MODE includes password:

- SUPERADMIN_PASSWORD set   -> env var is authoritative, synced on every boot.
- SUPERADMIN_PASSWORD unset -> the superadmin owns their password. While they
  have none, each boot logs how to mint a setup link (the CLI command) - never
  a link or token: log access must not be enough to claim the instance.

The negative cases are the important ones: no hint for an account that already
has a password, nor when the env var pins the password, nor in OTP-only mode;
and boot itself never mints a token.
"""

import logging
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    create_password_reset_token,
    hash_password,
    initialize_default_roles,
    initialize_superadmin,
    verify_password,
)
from app.core.config import settings
from app.models.user import PasswordResetToken, User

pytestmark = pytest.mark.usefixtures("_bootstrap")


@pytest_asyncio.fixture
async def _bootstrap(db: AsyncSession, monkeypatch):
    """Password mode, no env password, and the superadmin row already present
    with no password — the state every install is in right after migrations.
    (Pre-creating the row keeps the tests independent of whether the test DB
    already has admins, which suppresses initialize_superadmin's auto-create.)"""
    monkeypatch.setattr(settings, "superadmin_email", f"root-{uuid.uuid4().hex[:8]}@example.com")
    monkeypatch.setattr(settings, "superadmin_password", None)
    monkeypatch.setattr(settings, "auth_mode", "password")
    await initialize_default_roles(db)
    db.add(User(email=settings.superadmin_email))
    await db.flush()


async def _superadmin(db: AsyncSession) -> User:
    return (
        await db.execute(select(User).where(User.email == settings.superadmin_email))
    ).scalar_one()


async def _tokens(db: AsyncSession, user: User) -> list[PasswordResetToken]:
    return list(
        (
            await db.execute(
                select(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
            )
        ).scalars()
    )


def _setup_links(caplog) -> list[str]:
    """The boot-time setup hints logged."""
    return [r.getMessage() for r in caplog.records if "setup-link" in r.getMessage()]


class TestSetupLinkMode:
    """No SUPERADMIN_PASSWORD, no password yet."""

    async def test_first_boot_logs_how_to_get_a_link_but_no_token(self, db: AsyncSession, caplog):
        with caplog.at_level(logging.DEBUG):
            await initialize_superadmin(db)

        user = await _superadmin(db)
        assert user.password_hash is None
        # Boot mints nothing: the operator does, with the CLI
        assert await _tokens(db, user) == []

        links = _setup_links(caplog)
        assert len(links) == 1
        assert "python -m app.cli setup-link" in links[0]
        assert settings.superadmin_email in links[0]
        assert all("token=" not in r.getMessage() for r in caplog.records)

    async def test_hint_never_contains_a_live_token(self, db: AsyncSession, caplog):
        """Even with a link outstanding, nothing logged at boot carries it."""
        await initialize_superadmin(db)
        user = await _superadmin(db)
        plain, _ = await create_password_reset_token(db, str(user.id))
        from app.core.redis import get_redis

        await (await get_redis()).delete("sinas:setup-link-hint")
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            await initialize_superadmin(db)
        assert len(_setup_links(caplog)) == 1
        assert all(plain not in r.getMessage() for r in caplog.records)

    async def test_sibling_workers_log_the_hint_once(self, db: AsyncSession, caplog):
        """uvicorn workers run the lifespan concurrently; one line per boot."""
        with caplog.at_level(logging.WARNING, logger="app.core.auth"):
            await initialize_superadmin(db)
            await initialize_superadmin(db)
            await initialize_superadmin(db)
        assert len(_setup_links(caplog)) == 1

    async def test_existing_password_is_never_touched_and_no_link_issued(
        self, db: AsyncSession, caplog
    ):
        await initialize_superadmin(db)
        user = await _superadmin(db)
        user.password_hash = hash_password("chosen-in-the-ui")
        await db.flush()
        from app.core.redis import get_redis

        await (await get_redis()).delete("sinas:setup-link-hint")
        caplog.clear()  # drop the legitimate first-boot hint

        with caplog.at_level(logging.WARNING, logger="app.core.auth"):
            await initialize_superadmin(db)

        user = await _superadmin(db)
        assert verify_password("chosen-in-the-ui", user.password_hash)
        assert await _tokens(db, user) == []
        assert _setup_links(caplog) == []


class TestEnvPinnedMode:
    """SUPERADMIN_PASSWORD set: authoritative, no setup link."""

    async def test_env_password_is_applied_and_no_link_issued(
        self, db: AsyncSession, caplog, monkeypatch
    ):
        monkeypatch.setattr(settings, "superadmin_password", "from-env")
        with caplog.at_level(logging.WARNING, logger="app.core.auth"):
            await initialize_superadmin(db)
        user = await _superadmin(db)
        assert verify_password("from-env", user.password_hash)
        assert await _tokens(db, user) == []
        assert _setup_links(caplog) == []

    async def test_ui_change_reverts_loudly(self, db: AsyncSession, caplog, monkeypatch):
        monkeypatch.setattr(settings, "superadmin_password", "from-env")
        await initialize_superadmin(db)
        user = await _superadmin(db)
        user.password_hash = hash_password("changed-in-ui")
        await db.flush()

        with caplog.at_level(logging.WARNING, logger="app.core.auth"):
            await initialize_superadmin(db)

        user = await _superadmin(db)
        assert verify_password("from-env", user.password_hash)
        reverts = [r for r in caplog.records if "was reset to it" in r.getMessage()]
        assert len(reverts) == 1 and reverts[0].levelno == logging.WARNING
        # The secret itself must never reach the logs
        assert all("from-env" not in r.getMessage() for r in caplog.records)

    async def test_unchanged_password_is_not_rewritten(self, db: AsyncSession, caplog, monkeypatch):
        monkeypatch.setattr(settings, "superadmin_password", "from-env")
        await initialize_superadmin(db)
        before = (await _superadmin(db)).password_hash
        with caplog.at_level(logging.INFO, logger="app.core.auth"):
            await initialize_superadmin(db)
        assert (await _superadmin(db)).password_hash == before
        assert not [r for r in caplog.records if "SUPERADMIN_PASSWORD" in r.getMessage()]


class TestRedeemKillsSiblings:
    async def test_newest_link_wins_and_redeeming_kills_the_rest(self, db: AsyncSession, client):
        """Minting a link revokes the one before it; redeeming one leaves no
        other link for the account usable."""
        await initialize_superadmin(db)
        user = await _superadmin(db)
        first, _ = await create_password_reset_token(db, str(user.id))
        second, _ = await create_password_reset_token(db, str(user.id))

        r = await client.post(
            "/auth/password-reset", json={"reset_token": first, "new_password": "attacker-chosen"}
        )
        assert r.status_code == 400
        r = await client.post(
            "/auth/password-reset", json={"reset_token": second, "new_password": "operator-chosen"}
        )
        assert r.status_code == 204, r.text
        assert verify_password("operator-chosen", (await _superadmin(db)).password_hash)
        assert all(t.used_at is not None for t in await _tokens(db, user))


class TestOtpMode:
    async def test_otp_only_issues_nothing(self, db: AsyncSession, caplog, monkeypatch):
        monkeypatch.setattr(settings, "auth_mode", "otp")
        with caplog.at_level(logging.WARNING, logger="app.core.auth"):
            await initialize_superadmin(db)
        user = await _superadmin(db)
        assert user.password_hash is None
        assert await _tokens(db, user) == []
        assert _setup_links(caplog) == []
