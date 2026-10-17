"""Setup links, review round: links and setup sessions are distinct
(a session can never mint another session), and every token / password
change for a user is serialised on the user row, so a password change can't
miss a session created concurrently and concurrent mints or exchanges can't
leave two usable tokens.

The race tests use real connections and real commits (the `db` fixture's
single rolled-back transaction can't show two transactions contending for a
lock), like the OIDC concurrency test."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.auth import (
    RESET_LINK,
    SETUP_SESSION,
    apply_new_password,
    consume_password_reset_token,
    create_password_reset_token,
    hash_password,
    issue_reset_token_locked,
    lock_user_for_credentials,
    take_reset_token,
    verify_password,
)
from app.core.config import settings
from app.models.user import PasswordResetToken, User
from app.services import setup_link as setup_link_service
from app.services.setup_link import mint_setup_link
from tests.conftest import _test_db_url, auth_headers

EXCHANGE = "/auth/setup-link/exchange"
COMPLETE = "/auth/setup-link/complete"


@pytest.fixture(autouse=True)
def _password_mode(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "password")
    monkeypatch.setattr(settings, "domain", "sinas.example.test")
    monkeypatch.setattr(settings, "console_url", None)


@pytest_asyncio.fixture
async def fresh_user(db: AsyncSession) -> User:
    user = User(email=f"first-{uuid.uuid4().hex[:8]}@example.com")
    db.add(user)
    await db.flush()
    return user


def _token_from(url: str) -> str:
    return url.split("?token=", 1)[1]


async def _exchange(client, url: str) -> str:
    r = await client.post(EXCHANGE, json={"token": _token_from(url)})
    assert r.status_code == 200, r.text
    return r.json()["setup_token"]


class TestLinksAndSessionsAreNotInterchangeable:
    async def test_exchanging_a_setup_session_is_refused(self, db, client, fresh_user: User):
        url, _, _ = await mint_setup_link(db, fresh_user.email)
        session_token = await _exchange(client, url)

        r = await client.post(EXCHANGE, json={"token": session_token})
        assert r.status_code == 400
        assert r.json()["detail"] == setup_link_service.INVALID_LINK_MESSAGE
        # The refusal didn't spend it: the person can still finish
        r = await client.post(COMPLETE, json={"setup_token": session_token, "new_password": "first-password"})
        assert r.status_code == 200

    async def test_a_session_never_creates_another(self, db, fresh_user: User):
        url, _, _ = await mint_setup_link(db, fresh_user.email)
        session_token, _, _ = await setup_link_service.exchange_setup_link(db, _token_from(url))
        assert await setup_link_service.exchange_setup_link(db, session_token) is None
        sessions = (
            await db.execute(
                select(func.count()).select_from(PasswordResetToken).where(
                    PasswordResetToken.user_id == fresh_user.id,
                    PasswordResetToken.purpose == SETUP_SESSION,
                )
            )
        ).scalar_one()
        assert sessions == 1

    async def test_a_link_is_not_a_setup_session(self, db, client, fresh_user: User):
        url, _, _ = await mint_setup_link(db, fresh_user.email)
        r = await client.post(COMPLETE, json={"setup_token": _token_from(url), "new_password": "first-password"})
        assert r.status_code == 400
        assert r.json()["detail"] == setup_link_service.INVALID_LINK_MESSAGE
        await _exchange(client, url)  # not spent by the refusal

    async def test_password_reset_endpoint_takes_links_only(self, db, client, fresh_user: User):
        url, _, _ = await mint_setup_link(db, fresh_user.email)
        session_token = await _exchange(client, url)
        r = await client.post("/auth/password-reset", json={"reset_token": session_token, "new_password": "sneaky-pw"})
        assert r.status_code == 400
        await db.refresh(fresh_user)
        assert fresh_user.password_hash is None

    async def test_purpose_is_recorded(self, db, client, fresh_user: User):
        url, _, _ = await mint_setup_link(db, fresh_user.email)
        await _exchange(client, url)
        rows = (
            await db.execute(select(PasswordResetToken.purpose).where(PasswordResetToken.user_id == fresh_user.id))
        ).scalars()
        assert sorted(rows) == [RESET_LINK, SETUP_SESSION]


class TestPasswordChangesKillSessions:
    async def test_self_service_change_between_exchange_and_complete(self, db, client, fresh_user: User):
        """The user changes their password in another tab after opening the
        link: the setup session can't overwrite it."""
        fresh_user.password_hash = hash_password("old-password")
        await db.flush()
        url, _, _ = await mint_setup_link(db, fresh_user.email)
        session_token = await _exchange(client, url)

        r = await client.post(
            "/auth/change-password",
            json={"current_password": "old-password", "new_password": "changed-elsewhere"},
            headers=auth_headers(fresh_user),
        )
        assert r.status_code == 204, r.text

        r = await client.post(COMPLETE, json={"setup_token": session_token, "new_password": "from-the-link"})
        assert r.status_code == 400
        await db.refresh(fresh_user)
        assert verify_password("changed-elsewhere", fresh_user.password_hash)

    async def test_admin_reset_between_exchange_and_complete(self, db, client, fresh_user: User):
        url, _, _ = await mint_setup_link(db, fresh_user.email)
        session_token = await _exchange(client, url)
        new_link, _ = await create_password_reset_token(db, str(fresh_user.id))  # an admin re-issues
        r = await client.post("/auth/password-reset", json={"reset_token": new_link, "new_password": "via-admin-link"})
        assert r.status_code == 204
        r = await client.post(COMPLETE, json={"setup_token": session_token, "new_password": "from-the-link"})
        assert r.status_code == 400
        await db.refresh(fresh_user)
        assert verify_password("via-admin-link", fresh_user.password_hash)

    async def test_env_pinned_superadmin_password_revokes_links(self, db, fresh_user: User, monkeypatch):
        from app.core.auth import initialize_default_roles, initialize_superadmin

        await initialize_default_roles(db)
        monkeypatch.setattr(settings, "superadmin_email", fresh_user.email)
        monkeypatch.setattr(settings, "superadmin_password", None)
        url, _, _ = await mint_setup_link(db, fresh_user.email)
        monkeypatch.setattr(settings, "superadmin_password", "pinned-by-env")
        await initialize_superadmin(db)
        assert await consume_password_reset_token(db, _token_from(url)) is None


# ------------------------------------------------------------------ races


@asynccontextmanager
async def _committed_user(password_hash: str | None = None):
    engine = create_async_engine(_test_db_url)
    email = f"race-{uuid.uuid4().hex[:8]}@example.com"
    try:
        async with AsyncSession(engine, expire_on_commit=False) as setup:
            user = User(email=email, password_hash=password_hash)
            setup.add(user)
            await setup.commit()
        yield engine, user
    finally:
        async with AsyncSession(engine) as cleanup:
            uid = select(User.id).where(User.email == email).scalar_subquery()
            await cleanup.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == uid))
            await cleanup.execute(delete(User).where(User.email == email))
            await cleanup.commit()
        await engine.dispose()


async def _usable(engine, user: User, purpose: str | None = None) -> int:
    async with AsyncSession(engine) as s:
        q = select(func.count()).select_from(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > datetime.now(UTC),
        )
        if purpose:
            q = q.where(PasswordResetToken.purpose == purpose)
        return (await s.execute(q)).scalar_one()


class TestRaces:
    async def test_password_change_mid_exchange_leaves_no_usable_session(self):
        """Exchange has consumed the link and is creating the session when a
        password change arrives: the change waits on the user lock, then
        revokes the session the exchange committed."""
        async with _committed_user(hash_password("old-password")) as (engine, user):
            async with AsyncSession(engine, expire_on_commit=False) as s:
                link, _ = await create_password_reset_token(s, str(user.id))

            exchanging = AsyncSession(engine, expire_on_commit=False)
            changing = AsyncSession(engine, expire_on_commit=False)
            try:
                # exchange_setup_link step by step: the link consumed under the lock...
                assert await take_reset_token(exchanging, link, RESET_LINK) is not None
                other = await changing.get(User, user.id)
                change = asyncio.create_task(apply_new_password(changing, other, hash_password("changed")))
                await asyncio.sleep(0.5)
                assert not change.done(), "the password change didn't wait for the exchange"
                # ...and the session created in the same transaction
                session_token, _ = await issue_reset_token_locked(exchanging, user.id, SETUP_SESSION)
                await exchanging.commit()
                await asyncio.wait_for(change, timeout=10)
            finally:
                await exchanging.close()
                await changing.close()

            assert await _usable(engine, user) == 0
            async with AsyncSession(engine) as s:
                assert await setup_link_service.redeem_setup_session(s, session_token) is None
                assert verify_password("changed", (await s.get(User, user.id)).password_hash)

    async def test_password_change_mid_complete_lands_after_it(self):
        """Complete holds the lock from redeeming the session until the
        password is set; a concurrent change waits and lands after it."""
        async with _committed_user() as (engine, user):
            async with AsyncSession(engine, expire_on_commit=False) as s:
                url, _, _ = await mint_setup_link(s, user.email)
            async with AsyncSession(engine, expire_on_commit=False) as s:
                session_token, _, _ = await setup_link_service.exchange_setup_link(s, _token_from(url))

            completing = AsyncSession(engine, expire_on_commit=False)
            changing = AsyncSession(engine, expire_on_commit=False)
            try:
                redeemed = await setup_link_service.redeem_setup_session(completing, session_token)
                assert redeemed is not None
                other = await changing.get(User, user.id)
                change = asyncio.create_task(apply_new_password(changing, other, hash_password("later")))
                await asyncio.sleep(0.5)
                assert not change.done(), "the password change didn't wait for complete"
                await setup_link_service.finish_setup(completing, redeemed, hash_password("from-link"))
                await asyncio.wait_for(change, timeout=10)
            finally:
                await completing.close()
                await changing.close()

            async with AsyncSession(engine) as s:
                assert verify_password("later", (await s.get(User, user.id)).password_hash)
            assert await _usable(engine, user) == 0

    async def test_concurrent_mints_leave_exactly_one_usable_link(self):
        async with _committed_user() as (engine, user):
            first = AsyncSession(engine, expire_on_commit=False)
            second = AsyncSession(engine, expire_on_commit=False)
            try:
                # The first mint has revoked and inserted, not yet committed...
                await lock_user_for_credentials(first, user.id)
                first_token, _ = await issue_reset_token_locked(first, user.id, RESET_LINK)
                racing = asyncio.create_task(create_password_reset_token(second, str(user.id)))
                await asyncio.sleep(0.5)
                assert not racing.done(), "the second mint didn't wait for the first"
                await first.commit()
                second_token, _ = await asyncio.wait_for(racing, timeout=10)
            finally:
                await first.close()
                await second.close()

            assert await _usable(engine, user, RESET_LINK) == 1
            async with AsyncSession(engine) as s:
                assert await consume_password_reset_token(s, first_token) is None
                assert await consume_password_reset_token(s, second_token) is not None

            # Many at once: still exactly one.
            sessions = [AsyncSession(engine, expire_on_commit=False) for _ in range(5)]
            try:
                await asyncio.gather(*(create_password_reset_token(s, str(user.id)) for s in sessions))
            finally:
                for s in sessions:
                    await s.close()
            assert await _usable(engine, user, RESET_LINK) == 1

    async def test_concurrent_exchange_of_one_link_yields_one_session(self):
        async with _committed_user() as (engine, user):
            async with AsyncSession(engine, expire_on_commit=False) as s:
                url, _, _ = await mint_setup_link(s, user.email)
            link = _token_from(url)
            sessions = [AsyncSession(engine, expire_on_commit=False) for _ in range(5)]
            try:
                results = await asyncio.gather(
                    *(setup_link_service.exchange_setup_link(s, link) for s in sessions)
                )
            finally:
                for s in sessions:
                    await s.close()
            assert sum(r is not None for r in results) == 1
            assert await _usable(engine, user, SETUP_SESSION) == 1
