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
