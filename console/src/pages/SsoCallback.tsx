import { useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Loader2 } from 'lucide-react';
import { useAuth } from '../lib/auth-context';
import { getApiErrorMessage } from '../lib/api';
import { returnPath } from '../lib/returnPath';
import { takeSsoVerifier } from '../lib/sso';

/** Where the backend sends the browser after the identity provider: with a
 * one-time code to trade for tokens, or with an error to show on the login
 * page. A tab that is already signed in (another tab signed in meanwhile)
 * shows the error here instead: the login page would send it straight on
 * to the dashboard, and the refusal would go unseen. */
export function SsoCallback() {
  const { completeSso, loading, user, logout } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  // Once: StrictMode runs effects twice, and the code is single use.
  const started = useRef(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // After the stored session (if any) has been checked: that check must
    // not land on top of the session this sign-in starts.
    if (loading || started.current) return;
    started.current = true;
    const code = params.get('code');
    const error = params.get('error');
    const verifier = takeSsoVerifier();
    const fail = (message: string) =>
      user ? setError(message) : navigate('/login', { replace: true, state: { ssoError: message } });

    if (error || !code) {
      fail(error || 'Single sign-on did not complete. Try again.');
      return;
    }
    if (!verifier) {
      fail('This sign-in was started in another tab or window. Start again here.');
      return;
    }
    completeSso(code, verifier)
      .then((returnTo) => navigate(returnPath({ from: returnTo }), { replace: true }))
      .catch((err) => fail(getApiErrorMessage(err, 'Signing in failed. Try again.')));
  }, [completeSso, loading, navigate, params, user]);

  if (error) {
    return (
      <div className="min-h-screen bg-surface-page flex items-center justify-center p-4">
        <div className="max-w-md w-full bg-surface-1 border border-line-soft rounded-lg p-6 space-y-4">
          <h1 className="text-lg font-semibold text-gray-100">Single sign-on failed</h1>
          <p className="text-sm text-red-400">{error}</p>
          {user && (
            <p className="text-sm text-gray-400">
              This browser is still signed in as <span className="text-gray-200">{user.email}</span>.
            </p>
          )}
          <div className="flex gap-2 justify-end">
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={async () => {
                await logout();
                navigate('/login', { replace: true, state: { ssoError: error } });
              }}
            >
              Sign out
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => navigate('/', { replace: true })}
            >
              Continue as {user?.email ?? 'current user'}
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-surface-page flex items-center justify-center">
      <div className="flex items-center gap-3 text-gray-400">
        <Loader2 className="w-5 h-5 animate-spin" />
        Signing you in...
      </div>
    </div>
  );
}
