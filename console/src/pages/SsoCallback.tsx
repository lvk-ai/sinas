import { useEffect, useRef } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Loader2 } from 'lucide-react';
import { useAuth } from '../lib/auth-context';
import { getApiErrorMessage } from '../lib/api';
import { returnPath } from '../lib/returnPath';
import { takeSsoVerifier } from '../lib/sso';

/** Where the backend sends the browser after the identity provider: with a
 * one-time code to trade for tokens, or with an error to show on the login
 * page. */
export function SsoCallback() {
  const { completeSso, loading } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  // Once: StrictMode runs effects twice, and the code is single use.
  const started = useRef(false);

  useEffect(() => {
    // After the stored session (if any) has been checked: that check must
    // not land on top of the session this sign-in starts.
    if (loading || started.current) return;
    started.current = true;
    const code = params.get('code');
    const error = params.get('error');
    const verifier = takeSsoVerifier();
    const fail = (message: string) => navigate('/login', { replace: true, state: { ssoError: message } });

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
  }, [completeSso, loading, navigate, params]);

  return (
    <div className="min-h-screen bg-surface-page flex items-center justify-center">
      <div className="flex items-center gap-3 text-gray-400">
        <Loader2 className="w-5 h-5 animate-spin" />
        Signing you in...
      </div>
    </div>
  );
}
