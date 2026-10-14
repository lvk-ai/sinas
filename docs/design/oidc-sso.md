# SSO login via OpenID Connect

- **Status:** Accepted, implemented on `feat/oidc-sso`
- **Date:** 2026-10-14
- **Authors:** Kjeld Oostra, Claude
- **Related code:**
  - `backend/app/services/sso.py`: discovery, ID-token verification, claims, account linking, role sync, enforcement, login state
  - `backend/app/api/runtime/endpoints/sso.py`: `/auth/oidc/*` (public login flow)
  - `backend/app/api/v1/endpoints/identity_providers.py`: admin CRUD
  - `backend/app/services/resources/identity_providers.py`: the applier (the `identityProviders` kind)
  - `backend/app/models/identity_provider.py`, migration `o1i2d3c4s5s6`
  - `console/src/pages/Login.tsx`, `SsoCallback.tsx`, `IdentityProviders.tsx`
- **Related docs:** `docs-mint/admin/sso.mdx`, `docs/design/config-apply-unification.md`

## 1. Context

The console signs people in with an emailed one-time code, a password, or
both (`AUTH_MODE`). Organisations that already run an identity provider
(Keycloak, Auth0, WorkOS, Entra ID, Okta, ...) want their people to sign in
there, and want what those people may do in Sinas to follow the groups they
hold at the IdP.

Some of the parts already exist:

- `user_identities`: `(provider, subject)` linked to a user;
- `POST /auth/token/exchange`: a trusted backend asserts an identity and
  gets Sinas tokens (auto-provisioning with `TOKEN_EXCHANGE_DEFAULT_ROLE`);
- RS256 access tokens and `/.well-known/jwks.json`;
- `Role.email_domain`, stored but never acted on;
- refresh tokens kept in the database, revocable.

What's missing is a login flow that lets the browser go to the IdP and come
back signed in.

## 2. Decisions

1. **Generic OpenID Connect only.** Authorization Code flow with PKCE (S256),
   endpoints found by discovery (`{issuer}/.well-known/openid-configuration`).
   No vendor-specific code paths and no SAML: a SAML-only directory is put
   behind an IdP that speaks OIDC (Keycloak, Auth0, WorkOS and Okta all
   broker SAML).

2. **Identity providers are a resource kind**, `identityProviders`, on the
   applier framework, like every other configurable kind: REST CRUD,
   config apply and export, change history and restore, admin-only
   permissions (`sinas.identity_providers.*`), a console page. A provider has:

   | Field | Notes |
   |---|---|
   | `name` | key; in the callback URL, so it can't be renamed |
   | `displayName` | the "Sign in with ..." label |
   | `issuer` | `https://`; `http://` only for localhost or with `OIDC_ALLOW_HTTP` |
   | `clientId` | |
   | `clientSecret` | the **name** of a shared Secret, never the value; empty for a public client |
   | `scopes` | default `openid email profile`; must include `openid` |
   | `emailClaim` | path, default `email` |
   | `groupsClaim` | path, default `groups`; see 2.1 |
   | `roleMappings` | rules, see §4 |
   | `allowedDomains` | email domains that may sign in; empty is any |
   | `autoProvision` | create unknown users (default off) |
   | `defaultRole` | given once to a user this provider creates |
   | `enforceForDomains` | domains that must use this provider, see §6 |
   | `sessionHours` | cap on an SSO session, default 12, see §5 |
   | `isActive` | |

   Packages can't declare identity providers (they are skipped like LLM
   providers and package repositories): a package must never be able to add
   a way to sign in.

   **2.1 Claim paths.** `realm_access.roles` walks into nested objects. A key
   may itself contain dots or slashes (Auth0's namespaced claims, e.g.
   `https://example.com/roles`), so a path is resolved by trying the
   longest run of dot-separated segments that is a key at each level, then
   shorter ones. No escaping needed: `https://example.com/roles` is found as
   one key, `https://example.com/claims.roles` as a key and then `roles`
   inside it. A string value counts as a one-element list.

3. **Login flow.**

   ```
   console                         backend                              IdP
   GET /auth/oidc/providers  ───▶  [{name, display_name}]
   (button) navigate to
   /auth/oidc/{name}/start?code_challenge=C&return_to=/agents
                              ───▶ state, nonce, PKCE verifier → Redis (10 min)
                                   bind cookie (HttpOnly, path /auth/oidc)
                                   302 ─────────────────────────────▶ /authorize
                                                                       (user signs in)
                              ◀─── /auth/oidc/{name}/callback?code&state ◀── 302
                                   consume state (GETDEL), check cookie
                                   code → tokens (token endpoint, PKCE verifier)
                                   verify ID token (JWKS); userinfo if needed
                                   link / provision; sync roles; commit
                                   one-time code → Redis (60 s)
   /ui/sso/callback?code=X    ◀─── 302
   POST /auth/oidc/exchange {code, code_verifier}
                              ───▶ GETDEL code, check S256(verifier) == C
                              ◀─── {access_token, refresh_token, user, return_to}
   ```

   - The redirect URI registered at the IdP is the backend's
     `{public base URL}/auth/oidc/{name}/callback` (the console's admin page
     shows it with a copy button).
   - Where the browser ends up is never taken from the request: the backend
     always redirects to `{public console URL}/sso/callback`. `return_to` is
     only a path inside the console (`/...`, not `//...`), handed back by the
     exchange and followed by the console.
   - Errors (denied consent, bad state, a refused account) redirect to the
     same console route with `?error=<message>`; the messages carry no
     token, claim dump or secret.
   - The ID token is verified against the IdP's JWKS: signature (asymmetric
     algorithms only), `iss` (the discovered issuer, exactly), `aud`
     (contains the client id; `azp` must be the client id when there are
     several audiences), `exp`/`iat`/`nbf` with 60 s leeway, `nonce`, and
     `at_hash` when present. Discovery and JWKS are cached per process for
     an hour; a `kid` not in the cached set refetches the JWKS once (key
     rotation), at most once a minute.
   - Claims come from the ID token. If the email or groups claim isn't
     there and the IdP has a userinfo endpoint, it fills in what is missing
     (its `sub` must match the ID token's).

   **Account resolution.** Users are linked in `user_identities` by
   `(issuer, sub)`. The issuer, not the provider's name: subjects are only
   unique per issuer, so pointing a provider at a different IdP never
   matches an old subject to a new person.
   1. A linked identity: that user (refused if deactivated).
   2. Else an existing user with the claimed email, **only** when the IdP
      says `email_verified: true`. Also refused when that user already has
      a different subject at this issuer, and for the superadmin (link the
      superadmin's identity explicitly through the users API if wanted).
      The user row is locked from that check until the login commits, so
      two first sign-ins with different subjects for one address can't both
      link. (Not a unique index on (user, provider): identities linked by
      token exchange or by hand may legitimately hold several subjects for
      one provider name.)
   3. Else, with `autoProvision`, a new user with that email (and
      `defaultRole`). Not when the IdP says the email is unverified.
   4. Else refused: "no Sinas account".
   `allowedDomains`, when set, applies to every sign-in (linked, linked by
   email, and provisioned).

4. **Role sync.** Each rule maps a claim value to Sinas role names:

   ```yaml
   roleMappings:
     - group: sinas-admins         # a value in the groups claim (exact)
       roles: [Admins]
     - emailDomain: example.com    # the user's email domain
       roles: [Users]
   ```

   `emailDomain` is what `Role.email_domain` was meant to be, as a rule on
   the provider (the column stays as stored data). Memberships an IdP
   mapping grants are tagged on the row: `user_roles.source = "idp:<name>"`
   (`NULL` = given by hand, by config, or by provisioning). At each SSO
   login, under a lock on the user row:
   - every mapped role the user doesn't hold actively is granted, tagged
     with this provider (an ended membership row is reactivated and
     re-tagged);
   - every active membership tagged with **this** provider whose role no
     longer maps is ended (`active = false`, `removed_at`), the way the API
     ends memberships;
   - a membership given by hand, by config, or by another provider is never
     touched, even when this provider maps the same role. If it maps a role
     the user already holds from elsewhere, nothing is tagged.
   Granting by hand clears the tag when it reactivates a row, so a manual
   grant is never later removed by a sync. A role name that doesn't exist
   is refused when the provider is written; one deleted since is skipped
   at login with a warning. Changes are logged (user id, provider,
   roles added/removed). Memberships are not an applier kind, so they have
   no change-history entries; the provider's own changes do.

   Known limits: when two providers map the same role, it's tagged by
   whichever granted it first, and only that one removes it. Config apply's
   `users[].roles` is the full set for a config-managed user, so it may end
   IdP-granted memberships (they come back at the next SSO login).

5. **Sessions.** An SSO session ends `sessionHours` after the login, to the
   second. Its refresh token expires then (never later than
   `REFRESH_TOKEN_EXPIRE_DAYS`) and records the provider
   (`refresh_tokens.identity_provider`). Every access token minted for the
   session, at the exchange or by a refresh, expires at the usual lifetime
   or at the session's end, whichever comes first, so a refresh just before
   the deadline doesn't stretch the session by a full access token. After
   that the console's refresh fails, the user goes through the IdP again,
   and roles are synced again. A user removed or deactivated at the IdP
   keeps no access past the session's end. Password and OTP sessions are
   unchanged.

6. **The operator can't be locked out.**
   - Password and OTP login keep working; SSO is an addition.
   - `enforceForDomains`: a user whose email domain is listed on an active
     provider gets `403 Accounts at <domain> sign in with <provider>` from
     `/auth/login` and `/auth/verify-otp`, and a refresh token that didn't
     come from SSO stops working for them. The check is by domain, so it
     reveals nothing about whether an account exists.
   - The superadmin (`SUPERADMIN_EMAIL`) is always exempt.
   - The console's email box asks `POST /auth/oidc/discover` first and
     sends an enforced domain straight to its provider.

7. **Out of scope** (follow-ups, §9): SCIM provisioning, RP-initiated
   logout, IdP-initiated login.

## 3. Data model

- `identity_providers`: the fields above plus `user_id`, `managed_by`,
  `config_name`, `config_checksum`, timestamps. Lists are JSON.
- `user_roles.source` (text, nullable): who granted a membership, when it
  wasn't by hand.
- `refresh_tokens.identity_provider` (text, nullable): the provider whose
  login issued the token.

One migration, additive.

## 4. Threat model

| Threat | Mitigation |
|---|---|
| CSRF on the callback / login CSRF (an attacker's code completed in a victim's browser) | `state` is random, single-use (Redis `GETDEL`), 10-minute TTL, and bound to the starting browser by an HttpOnly `SameSite=Lax` cookie on the backend's origin (`/auth/oidc` path), compared in constant time. |
| Authorization code interception | PKCE S256 between backend and IdP; confidential clients also authenticate with the client secret. |
| ID-token replay / substitution | `nonce` stored with the state and checked; `aud`/`azp`, `iss`, expiry checked; signature by the IdP's published keys only, asymmetric algorithms only (no `alg: none`, no HMAC with the client secret, so no algorithm confusion). |
| Account takeover through a claimed email | Linking to an existing account by email only with `email_verified: true`; never onto a user who already has another subject at the issuer; never onto the superadmin. An explicitly unverified email can't provision, satisfy `allowedDomains`, or match an `emailDomain` rule. |
| Subject collision after re-pointing a provider | Identities keyed by issuer, not provider name. |
| Open redirect | The final redirect target is fixed (`{console}/sso/callback`); `return_to` must be a console path and is returned by the exchange, never redirected to by the backend. |
| Tokens in URLs, history, Referer, logs | Only a one-time code is in the URL: 32 random bytes, single use (`GETDEL`), 60-second TTL, and bound to the console tab by a second PKCE pair (the console keeps the verifier in `sessionStorage`). Tokens are minted at the exchange; nothing is stored in Redis but the user id. |
| Client secret exposure | Stored only as a shared Secret (encrypted), resolved at the token request, never returned or logged; the provider row and its history hold the Secret's name. |
| A package adding a sign-in path | `identityProviders` is skipped for packages. |
| Privilege drift after IdP changes | Session cap + role sync at every login. |
| Lock-out | Superadmin exempt from enforcement; enforcement is per domain and opt-in. |

## 5. API

Public (runtime, under `/auth/oidc`):
- `GET /providers`: `[{name, display_name}]` of active providers.
- `POST /discover` `{email}`: `{provider}` if the domain is enforced, else null.
- `GET /{name}/start?code_challenge=&return_to=`: 302 to the IdP.
- `GET /{name}/callback`: the IdP's redirect target; 302 to the console.
- `POST /exchange` `{code, code_verifier}`: the token pair.

Admin (`/api/v1/identity-providers`): `GET`, `POST`, `GET /{name}`,
`PUT /{name}` (partial, no rename), `DELETE /{name}`. Responses include
`redirect_uri`.

## 6. Configuration

- `OIDC_ALLOW_HTTP` (default false): allow `http://` issuers other than
  localhost (labs only).
- The backend's public URL (`DOMAIN`) and the console URL (`CONSOLE_URL`)
  must be right: they form the redirect URI and the console callback.

## 7. Testing

Unit tests mock the IdP with an httpx `MockTransport`, sign ID tokens with a
generated RSA key and publish it as a JWKS. An end-to-end script runs a real
Keycloak and a throwaway Sinas backend in Docker (`tests/e2e/oidc/`).

## 8. Alternatives considered

- **Tokens in the redirect fragment.** Simpler, but tokens end up in browser
  history and in whatever reads the URL. The one-time code costs one request.
- **Provider name as the identity's provider.** Breaks the subject
  uniqueness OIDC gives (per issuer) when a provider is re-pointed.
- **A separate memberships table per source.** The `source` tag on the
  existing row keeps every reader of `user_roles` unchanged.

## 9. Follow-ups

- **SCIM** (phase 2): provisioning and deprovisioning pushed by the IdP, so
  a user removed there is deactivated at once rather than within
  `sessionHours`, and group changes apply without a login.
- **RP-initiated logout**: end the IdP session on console logout
  (`end_session_endpoint`). Back-channel logout to end Sinas sessions when
  the IdP's ends.
- **IdP-initiated login** (a tile in the IdP's app launcher): needs a
  login-initiation endpoint that starts a fresh flow, never accepting an
  unsolicited response.
- Mapping rules on arbitrary claims (`claim` + `value`), and patterns.
- Per-provider "trust email" for IdPs that never send `email_verified`
  (Entra ID), so existing accounts can be linked by email there too.
