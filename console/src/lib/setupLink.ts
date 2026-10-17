/** Setup links: `<console>/ui/?token=...` (also `/ui?token=`, and the older
 * `/ui/reset-password?token=` form). The token is taken out of the address
 * bar before anything renders or loads, kept in memory only, and traded for a
 * short-lived setup session on the /setup page. */

/** Where the setup page lives, inside the router's basename. */
export const SETUP_ROUTE = '/setup';

/** Console paths (below the basename) that accept `?token=` as a setup link. */
const TOKEN_PATHS = new Set(['', '/', SETUP_ROUTE, '/reset-password']);

/** The setup token in this URL, if it is a setup link, and the address to
 * replace it with (same page minus `token`, on the setup route). Pure, for
 * tests. `base` is the router basename, e.g. "/ui". */
export function parseSetupLink(
  pathname: string,
  search: string,
  hash: string,
  base: string,
): { token: string; cleanUrl: string } | null {
  const root = base.replace(/\/+$/, '');
  if (pathname !== root && !pathname.startsWith(`${root}/`)) return null;
  const inner = pathname.slice(root.length).replace(/\/+$/, '') || '/';
  if (!TOKEN_PATHS.has(inner)) return null;
  const params = new URLSearchParams(search);
  const token = params.get('token');
  if (!token) return null;
  params.delete('token');
  const rest = params.toString();
  return { token, cleanUrl: `${root}${SETUP_ROUTE}${rest ? `?${rest}` : ''}${hash}` };
}

let pendingToken: string | null = null;

/** Run once, before the app renders: move a setup link's token from the
 * address bar into memory (history.replaceState: the token never stays in
 * the tab's history or address bar, nor in Referers of later requests). */
export function captureSetupLink(base: string): void {
  const { pathname, search, hash } = window.location;
  const parsed = parseSetupLink(pathname, search, hash, base);
  if (!parsed) return;
  pendingToken = parsed.token;
  window.history.replaceState(window.history.state, '', parsed.cleanUrl);
}

/** The captured link token, once. */
export function takeSetupLinkToken(): string | null {
  const token = pendingToken;
  pendingToken = null;
  return token;
}

/** What an exchanged link leaves this tab: enough to set a password until
 * `expires_at`. sessionStorage: this tab's, survives a reload, gone with it. */
export interface SetupSession {
  setup_token: string;
  email: string;
  has_password: boolean;
  expires_at: string;
}

const SESSION_KEY = 'sinas_setup_session';

export function saveSetupSession(session: SetupSession): void {
  try {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
  } catch {
    // storage unavailable: the page still works until it is reloaded
  }
}

export function loadSetupSession(now: number = Date.now()): SetupSession | null {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const session = JSON.parse(raw) as SetupSession;
    if (!session.setup_token || new Date(session.expires_at).getTime() <= now) {
      sessionStorage.removeItem(SESSION_KEY);
      return null;
    }
    return session;
  } catch {
    return null;
  }
}

export function clearSetupSession(): void {
  try {
    sessionStorage.removeItem(SESSION_KEY);
  } catch {
    // ignore
  }
}

/** password+otp: the password is saved and a code was emailed; what the code
 * step needs to survive a reload. The OTP session id is no credential on its
 * own (the emailed code is), the same as the login page's code step. */
export interface PendingCode {
  session_id: string;
  email: string;
}

const PENDING_CODE_KEY = 'sinas_setup_pending_code';

export function savePendingCode(pending: PendingCode): void {
  try {
    sessionStorage.setItem(PENDING_CODE_KEY, JSON.stringify(pending));
  } catch {
    // storage unavailable: the code step works until a reload
  }
}

export function loadPendingCode(): PendingCode | null {
  try {
    const raw = sessionStorage.getItem(PENDING_CODE_KEY);
    if (!raw) return null;
    const pending = JSON.parse(raw) as PendingCode;
    return pending.session_id && pending.email ? pending : null;
  } catch {
    return null;
  }
}

/** What a failed "save password" (POST /auth/setup-link/complete) means for
 * the setup session kept in this tab:
 * - 'fix': the password was refused (422); fix it and resubmit.
 * - 'retry': nothing was spent (429 rate limit, no response, a 5xx other
 *   than 503); keep the session and the form, try again in a moment.
 * - 'spent': the session is invalid or used (the generic 400, 403 SSO), or
 *   the password is saved and only the emailed code failed (503); this
 *   page can't go on. */
export function completeFailure(status: number | undefined): 'fix' | 'retry' | 'spent' {
  if (status === 422) return 'fix';
  if (status === undefined || status === 429 || (status >= 500 && status !== 503)) return 'retry';
  return 'spent';
}

/** The way out to the login page, given who (if anyone) this browser is
 * signed in as and which account the link was for. A signed-in browser
 * can't reach the login form (it bounces to the dashboard), so switching
 * accounts means signing out first, said out loud. */
export function signInAction(
  signedInAs: string | null | undefined,
  linkFor?: string,
): { label: string; signOut: boolean; toDashboard: boolean } {
  const same = !!signedInAs && !!linkFor && signedInAs.toLowerCase() === linkFor.toLowerCase();
  if (same) return { label: `Continue as ${signedInAs}`, signOut: false, toDashboard: true };
  if (signedInAs) {
    return {
      label: linkFor ? `Sign out and sign in as ${linkFor}` : 'Sign out and go to sign in',
      signOut: true,
      toDashboard: false,
    };
  }
  return { label: linkFor ? 'Sign in' : 'Go to sign in', signOut: false, toDashboard: false };
}

/** Where a reload of the setup page (no token in the URL) picks up: the code
 * step if the password is already saved, else the setup session, else
 * nothing (the link is spent or was never valid). */
export function resumeSetup(
  now: number = Date.now(),
): { step: 'code'; pending: PendingCode } | { step: 'password'; session: SetupSession } | null {
  const pending = loadPendingCode();
  if (pending) return { step: 'code', pending };
  const session = loadSetupSession(now);
  return session ? { step: 'password', session } : null;
}

export function clearPendingCode(): void {
  try {
    sessionStorage.removeItem(PENDING_CODE_KEY);
  } catch {
    // ignore
  }
}
