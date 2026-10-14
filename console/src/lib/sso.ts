import { API_BASE_URL } from './api';

/** Where this tab keeps the verifier of the challenge it started SSO with.
 * sessionStorage: one tab's, gone with it. */
const VERIFIER_KEY = 'sinas_sso_verifier';

function base64url(bytes: Uint8Array): string {
  let binary = '';
  bytes.forEach((b) => { binary += String.fromCharCode(b); });
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** Send the browser to the identity provider (through the backend's
 * /start). The one-time code it comes back with is only good together with
 * the verifier kept here, so a code seen elsewhere (history, a log) is no
 * use on its own. */
export async function startSso(provider: string, returnTo = '/'): Promise<void> {
  if (!window.crypto?.subtle) {
    throw new Error('Single sign-on needs a secure connection (https).');
  }
  const verifier = base64url(window.crypto.getRandomValues(new Uint8Array(32)));
  const digest = await window.crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  sessionStorage.setItem(VERIFIER_KEY, verifier);
  const params = new URLSearchParams({ code_challenge: base64url(new Uint8Array(digest)), return_to: returnTo });
  window.location.assign(`${API_BASE_URL}/auth/oidc/${encodeURIComponent(provider)}/start?${params}`);
}

/** The verifier this tab started with, once. */
export function takeSsoVerifier(): string | null {
  const verifier = sessionStorage.getItem(VERIFIER_KEY);
  sessionStorage.removeItem(VERIFIER_KEY);
  return verifier;
}
