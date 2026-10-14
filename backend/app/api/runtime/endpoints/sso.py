"""SSO login endpoints: OpenID Connect, Authorization Code + PKCE
(docs/design/oidc-sso.md). Public by design: this is how people sign in.

    GET  /auth/oidc/providers         the login page's buttons
    POST /auth/oidc/discover          does this email's domain have to use SSO?
    GET  /auth/oidc/{name}/start      -> the IdP
    GET  /auth/oidc/{name}/callback   <- the IdP; -> the console with a one-time code
    POST /auth/oidc/exchange          one-time code -> Sinas tokens
"""
import logging
import re
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import rate_limit_by_ip
from app.models import User
from app.models.identity_provider import IdentityProvider
from app.schemas.identity_provider import (
    SsoDiscoverRequest,
    SsoDiscoverResponse,
    SsoExchangeRequest,
    SsoExchangeResponse,
    SsoProviderInfo,
)
from app.services import sso

router = APIRouter()
logger = logging.getLogger(__name__)

# Per client IP and rate-limit window. Generous: a whole office signing in
# at nine shares one address, and nothing here can be guessed (states and
# codes are 256 random bits); this only bounds what one client parks in
# Redis.
SSO_IP_LIMIT = 300


def _to_console(**params: str) -> RedirectResponse:
    """Back to the console's SSO route: the only place this flow redirects
    a browser to, besides the IdP."""
    response = RedirectResponse(f"{sso.console_callback_url()}?{urlencode(params)}", status_code=302)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _failed(message: str) -> RedirectResponse:
    response = _to_console(error=message)
    response.delete_cookie(sso.BIND_COOKIE_NAME, path=sso.BIND_COOKIE_PATH)
    return response


async def _active(db: AsyncSession, name: str) -> IdentityProvider | None:
    return (
        await db.execute(
            select(IdentityProvider).where(IdentityProvider.name == name, IdentityProvider.is_active.is_(True))
        )
    ).scalar_one_or_none()


@router.get("/oidc/providers", response_model=list[SsoProviderInfo])
async def list_sso_providers(db: AsyncSession = Depends(get_db)):
    """Active identity providers, for "Sign in with ..." buttons."""
    return [SsoProviderInfo(name=p.name, display_name=p.display_name) for p in await sso.active_providers(db)]


@router.post("/oidc/discover", response_model=SsoDiscoverResponse)
async def discover_sso_provider(body: SsoDiscoverRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """The provider an email's domain must sign in with, if any (the console
    sends such a domain straight there). Decided by domain alone, so it
    reveals nothing about accounts."""
    await rate_limit_by_ip(request, "sso_discover", SSO_IP_LIMIT, settings.rate_limit_window_seconds)
    provider = await sso.enforcing_provider(db, body.email)
    if provider is None:
        return SsoDiscoverResponse()
    return SsoDiscoverResponse(provider=SsoProviderInfo(name=provider.name, display_name=provider.display_name))


@router.get("/oidc/{name}/start", include_in_schema=False)
async def start_sso(
    name: str,
    request: Request,
    code_challenge: str = Query(default=""),
    return_to: str = Query(default="/"),
    db: AsyncSession = Depends(get_db),
):
    """Send the browser to the IdP. `code_challenge` (S256 of a verifier the
    console keeps) binds the one-time code to the console tab; `return_to`
    is a console path to continue on afterwards."""
    await rate_limit_by_ip(request, "sso_start", SSO_IP_LIMIT, settings.rate_limit_window_seconds)
    if not sso.valid_challenge(code_challenge):
        return _failed("This sign-in link is incomplete. Start again from the login page.")
    provider = await _active(db, name)
    if provider is None:
        return _failed("This sign-in option isn't available.")
    try:
        doc = await sso.discovery(provider.issuer)
    except sso.SsoError as e:
        return _failed(str(e))

    verifier = secrets.token_urlsafe(64)  # PKCE between us and the IdP
    nonce = secrets.token_urlsafe(32)
    browser_nonce = secrets.token_urlsafe(32)
    state = await sso.store_state(
        sso.LoginState(
            provider=provider.name,
            nonce=nonce,
            code_verifier=verifier,
            browser_nonce=browser_nonce,
            console_challenge=code_challenge,
            return_to=sso.safe_return_to(return_to),
        )
    )
    response = RedirectResponse(
        sso.authorization_url(provider, doc, state=state, nonce=nonce, code_verifier=verifier), status_code=302
    )
    response.headers["Cache-Control"] = "no-store"
    # Binds the flow to this browser: a callback URL someone else started
    # (login CSRF) arrives without it. Lax: sent on the IdP's top-level
    # redirect back.
    response.set_cookie(
        sso.BIND_COOKIE_NAME,
        browser_nonce,
        max_age=sso.STATE_TTL_SECONDS,
        path=sso.BIND_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=settings.public_base_url.startswith("https://"),
    )
    return response


@router.get("/oidc/{name}/callback", include_in_schema=False)
async def sso_callback(
    name: str,
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
):
    """The IdP's redirect target. Verifies the sign-in, links or creates the
    user, syncs roles, and hands the console a one-time code."""
    flow = await sso.consume_state(state)
    if flow is None or flow.provider != name:
        return _failed("This sign-in has expired or was already used. Start again.")
    cookie = request.cookies.get(sso.BIND_COOKIE_NAME) or ""
    if not secrets.compare_digest(cookie, flow.browser_nonce):
        return _failed("This sign-in was started in another browser. Start again here.")
    if error:
        # Only the error code: a description is the IdP's (or anyone's) free text.
        code_name = re.sub(r"[^a-z_]", "", error.lower())[:64] or "error"
        return _failed(f"The identity provider didn't sign you in ({code_name}).")
    if not code:
        return _failed("The identity provider returned no sign-in code.")

    provider = await _active(db, name)
    if provider is None:
        return _failed("This sign-in option isn't available.")
    try:
        doc = await sso.discovery(provider.issuer)
        tokens = await sso.exchange_code(db, provider, doc, code, flow.code_verifier)
        claims = await sso.verify_id_token(
            tokens.id_token, doc=doc, client_id=provider.client_id, nonce=flow.nonce,
            access_token=tokens.access_token,
        )
        if sso.needs_userinfo(provider, claims):
            claims = await sso.with_userinfo(doc, claims, tokens.access_token)
        outcome = await sso.sign_in(db, provider, doc["issuer"], claims)
        await db.commit()
    except sso.SsoError as e:
        await db.rollback()
        logger.info(f"SSO {name}: sign-in refused: {e}")
        return _failed(str(e))
    except IntegrityError:
        # A first sign-in racing another for the same email or identity.
        await db.rollback()
        return _failed("Signing in collided with another sign-in for this account. Try again.")

    user = outcome.user
    logger.info(
        f"SSO {name}: user {user.id} signed in"
        + (" (provisioned)" if outcome.provisioned else "")
        + (" (linked by verified email)" if outcome.linked_by_email else "")
    )
    one_time = await sso.store_code(
        user_id=str(user.id),
        provider=provider.name,
        session_hours=provider.session_hours,
        console_challenge=flow.console_challenge,
        return_to=flow.return_to,
    )
    response = _to_console(code=one_time)
    response.delete_cookie(sso.BIND_COOKIE_NAME, path=sso.BIND_COOKIE_PATH)
    return response


@router.post("/oidc/exchange", response_model=SsoExchangeResponse)
async def exchange_sso_code(body: SsoExchangeRequest, db: AsyncSession = Depends(get_db)):
    """Trade the one-time code from the redirect for Sinas tokens. Single
    use, a minute to live, and only with the verifier of the challenge the
    console started with. The refresh token lasts the provider's
    session_hours at most."""
    from app.api.runtime.endpoints.authentication import _issue_tokens

    payload = await sso.consume_code(body.code, body.code_verifier)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired sign-in code")
    user = await db.get(User, payload["user_id"])
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account is deactivated.")
    session = timedelta(hours=int(payload["session_hours"]))
    access_token, refresh_token, user_resp = await _issue_tokens(
        db, user, max_lifetime=session, identity_provider=payload["provider"],
    )
    return SsoExchangeResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=int(min(session, timedelta(minutes=settings.access_token_expire_minutes)).total_seconds()),
        user=user_resp,
        return_to=sso.safe_return_to(payload.get("return_to")),
    )
