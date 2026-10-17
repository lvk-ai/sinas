import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { CheckCircle2, KeyRound, Loader2, Lock } from 'lucide-react';
import { Logo } from '../components/Logo';
import ThemeToggle from '../components/ThemeToggle';
import { apiClient, getApiErrorMessage } from '../lib/api';
import { useAuth } from '../lib/auth-context';
import {
  clearPendingCode,
  clearSetupSession,
  completeFailure,
  resumeSetup,
  savePendingCode,
  saveSetupSession,
  signInAction,
  takeSetupLinkToken,
  type SetupSession,
} from '../lib/setupLink';

const INVALID = 'This link is invalid or has expired. Ask your administrator for a new one.';

type View =
  | { kind: 'checking' }
  | { kind: 'refused'; message: string }
  | { kind: 'has-password'; session: SetupSession }
  | { kind: 'form'; session: SetupSession }
  | { kind: 'otp'; sessionId: string; email: string };

/** Why a setup call failed, in words safe to show: the backend's own
 * message for the refusals it words generically (400, 403), never which
 * part of a link was wrong. */
function statusOf(err: unknown): number | undefined {
  return (err as { response?: { status?: number } } | null)?.response?.status;
}

function refusal(err: unknown): string {
  const status = statusOf(err);
  if (status === 429) return 'Too many attempts. Wait a few minutes and open the link again.';
  if (status === 400 || status === 403) return getApiErrorMessage(err, INVALID);
  // No answer or a server error: the link may well be unspent.
  if (status === undefined || status >= 500) return "Couldn't check your link just now. Open it again in a moment.";
  return INVALID;
}

/** `/ui/?token=...` lands here (lib/setupLink moved the token out of the
 * address bar). The link is spent right away for a setup session; the person
 * then sets a password and is signed in. A link never signs anyone in on its
 * own: with a password already on the account, it can only replace it. */
export function SetupLink() {
  const { completeSetup, verifyOTP, user, logout } = useAuth();
  const navigate = useNavigate();
  const started = useRef(false);  // once: StrictMode runs effects twice
  const [view, setView] = useState<View>({ kind: 'checking' });
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [otpCode, setOtpCode] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const token = takeSetupLinkToken();
    const show = (session: SetupSession) =>
      setView(session.has_password ? { kind: 'has-password', session } : { kind: 'form', session });

    if (!token) {
      // A reload of this page: the code step (password already saved), or
      // the session the link was traded for, if any.
      const resumed = resumeSetup();
      if (resumed?.step === 'code') {
        setView({ kind: 'otp', sessionId: resumed.pending.session_id, email: resumed.pending.email });
      } else if (resumed?.step === 'password') {
        show(resumed.session);
      } else {
        setView({ kind: 'refused', message: INVALID });
      }
      return;
    }
    // A new link starts over: nothing of an earlier link (another account,
    // maybe) may resume if this one fails.
    clearPendingCode();
    clearSetupSession();
    apiClient.exchangeSetupLink(token)
      .then((session) => {
        saveSetupSession(session);
        show(session);
      })
      .catch((err) => setView({ kind: 'refused', message: refusal(err) }));
  }, []);

  /** Leave for the login page (or, signed in as the link's own account, the
   * dashboard). Signed in as someone else: sign out first, or /login would
   * bounce straight back to their dashboard. */
  const toLogin = async (linkFor?: string) => {
    const action = signInAction(user?.email, linkFor);
    clearSetupSession();
    clearPendingCode();
    if (action.toDashboard) {
      navigate('/', { replace: true });
      return;
    }
    if (action.signOut) await logout();
    navigate('/login', { replace: true });
  };

  /** Which account this browser is signed in as, when it is: the buttons
   * below say what they do to it. */
  const signedInNote = user ? (
    <p className="text-sm text-gray-400">
      This browser is signed in as <span className="text-gray-200">{user.email}</span>.
    </p>
  ) : null;

  const signInButton = (linkFor?: string) => (
    <button type="button" onClick={() => toLogin(linkFor)} className="w-full btn btn-primary py-3 rounded-lg">
      {signInAction(user?.email, linkFor).label}
    </button>
  );

  const submitPassword = async (e: React.FormEvent, session: SetupSession) => {
    e.preventDefault();
    setError('');
    if (password.length < 8) {
      setError('Password must be at least 8 characters.');
      return;
    }
    if (new TextEncoder().encode(password).length > 72) {
      setError('Password must be at most 72 bytes.');
      return;
    }
    if (password !== confirm) {
      setError('Passwords do not match.');
      return;
    }
    setBusy(true);
    try {
      const otpSession = await completeSetup(session.setup_token, password);
      clearSetupSession();
      if (otpSession) {
        // Kept for a reload: the password is saved, only the code is left.
        savePendingCode({ session_id: otpSession, email: session.email });
        setView({ kind: 'otp', sessionId: otpSession, email: session.email });
      } else {
        navigate('/', { replace: true });
      }
    } catch (err) {
      const status = statusOf(err);
      const failure = completeFailure(status);
      if (failure === 'fix') {
        setError(getApiErrorMessage(err, 'Choose a different password.'));
      } else if (failure === 'retry') {
        // Nothing was spent: keep the session and the form.
        setError(
          status === 429
            ? 'Too many attempts. Wait a few minutes, then try again.'
            : "Couldn't save your password just now. Try again in a moment.",
        );
      } else {
        // The setup session is spent or gone (or, on 503, the password is
        // set and only the emailed code failed): this page can't go on.
        clearSetupSession();
        setView({
          kind: 'refused',
          message: status === 503 ? getApiErrorMessage(err, INVALID) : refusal(err),
        });
      }
    } finally {
      setBusy(false);
    }
  };

  const submitOtp = async (e: React.FormEvent, sessionId: string) => {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      await verifyOTP(sessionId, otpCode);
      clearPendingCode();
      navigate('/', { replace: true });
    } catch (err) {
      setError(getApiErrorMessage(err, 'Invalid code'));
    } finally {
      setBusy(false);
    }
  };

  const heading = (() => {
    switch (view.kind) {
      case 'checking': return 'Checking your link';
      case 'refused': return "This link can't be used";
      case 'has-password': return 'You already have a password';
      case 'form': return view.session.has_password ? 'Choose a new password' : 'Set your password';
      case 'otp': return 'One more step';
    }
  })();

  const inputClass =
    'w-full px-4 py-3 bg-surface-input border border-line rounded-lg text-gray-100 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent';

  return (
    <div className="min-h-screen bg-surface-page flex items-center justify-center p-4">
      <div className="absolute top-4 right-4">
        <ThemeToggle />
      </div>
      <div className="w-full max-w-md">
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center mb-4">
            <Logo className="h-12 w-auto" />
          </div>
          <h1 className="text-xl font-semibold text-gray-100">{heading}</h1>
        </div>

        <div className="bg-surface-1 rounded-2xl p-8 border border-line-soft">
          {view.kind === 'checking' && (
            <div className="flex items-center justify-center gap-3 text-gray-400">
              <Loader2 className="w-5 h-5 animate-spin" />
              One moment...
            </div>
          )}

          {view.kind === 'refused' && (
            <div className="space-y-6">
              <p className="text-sm text-gray-300">{view.message}</p>
              {signedInNote}
              {signInButton()}
            </div>
          )}

          {view.kind === 'has-password' && (
            <div className="space-y-6">
              <p className="text-sm text-gray-300">
                This link is for setting a password, and{' '}
                <span className="font-medium text-gray-100">{view.session.email}</span> already has one.
                Sign in with it as usual.
              </p>
              {signedInNote}
              {signInButton(view.session.email)}
              <button
                type="button"
                onClick={() => setView({ kind: 'form', session: view.session })}
                className="w-full text-sm text-gray-400 hover:text-gray-200"
              >
                Forgot it? Choose a new password instead
              </button>
            </div>
          )}

          {view.kind === 'form' && (
            <form onSubmit={(e) => submitPassword(e, view.session)} className="space-y-4">
              <p className="text-sm text-gray-400">
                For <span className="font-medium text-gray-100">{view.session.email}</span>. At least 8 characters.
              </p>
              {/* For password managers: the account this password belongs to. */}
              <input type="email" value={view.session.email} autoComplete="username" readOnly hidden />
              <div>
                <label htmlFor="new_password" className="block text-sm font-medium text-gray-300 mb-2">
                  New password
                </label>
                <div className="relative">
                  <KeyRound className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-gray-500" />
                  <input
                    id="new_password"
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    autoComplete="new-password"
                    autoFocus
                    required
                    minLength={8}
                    className={`${inputClass} pl-10`}
                  />
                </div>
              </div>
              <div>
                <label htmlFor="confirm_password" className="block text-sm font-medium text-gray-300 mb-2">
                  Confirm password
                </label>
                <input
                  id="confirm_password"
                  type="password"
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  autoComplete="new-password"
                  required
                  minLength={8}
                  className={inputClass}
                />
              </div>

              {error && (
                <div className="p-3 bg-red-900/20 border border-red-800/30 rounded-lg text-sm text-red-400">{error}</div>
              )}

              <button
                type="submit"
                disabled={busy || !password || !confirm}
                className="w-full btn btn-primary py-3 rounded-lg flex items-center justify-center disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {busy ? (
                  <>
                    <Loader2 className="w-5 h-5 mr-2 animate-spin" />
                    Saving...
                  </>
                ) : (
                  'Save password and sign in'
                )}
              </button>
            </form>
          )}

          {view.kind === 'otp' && (
            <form onSubmit={(e) => submitOtp(e, view.sessionId)} className="space-y-4">
              <div className="flex items-center gap-2 text-sm text-green-400">
                <CheckCircle2 className="w-4 h-4" />
                Password saved.
              </div>
              <p className="text-sm text-gray-400">
                We sent a code to <span className="font-medium text-gray-100">{view.email}</span>.
              </p>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-gray-500" />
                <input
                  id="otp"
                  type="text"
                  inputMode="numeric"
                  value={otpCode}
                  onChange={(e) => setOtpCode(e.target.value)}
                  placeholder="000000"
                  autoComplete="one-time-code"
                  aria-label="Verification code"
                  required
                  maxLength={6}
                  className={`${inputClass} pl-10 text-center text-2xl font-mono tracking-widest`}
                />
              </div>
              {error && (
                <div className="p-3 bg-red-900/20 border border-red-800/30 rounded-lg text-sm text-red-400">{error}</div>
              )}
              <button
                type="submit"
                disabled={busy || otpCode.length !== 6}
                className="w-full btn btn-primary py-3 rounded-lg flex items-center justify-center disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {busy ? <Loader2 className="w-5 h-5 animate-spin" /> : 'Verify and continue'}
              </button>
              {/* The code expired or was mistyped too often: the password is
                  saved, so a normal sign-in (with a fresh code) finishes it. */}
              {signedInNote}
              <button
                type="button"
                onClick={() => toLogin(view.email)}
                className="w-full text-sm text-gray-400 hover:text-gray-200"
              >
                {signInAction(user?.email, view.email).signOut
                  ? `Code expired? Sign out and sign in as ${view.email} with your new password`
                  : 'Code expired? Sign in with your new password'}
              </button>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
