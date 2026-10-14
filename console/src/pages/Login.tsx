import { useEffect, useRef, useState } from 'react';
import { Logo } from '../components/Logo';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { returnPath } from '../lib/returnPath';
import { useAuth } from '../lib/auth-context';
import { apiClient } from '../lib/api';
import { startSso } from '../lib/sso';
import type { SsoProvider } from '../types';
import { Mail, Lock, Loader2, KeyRound, LogIn } from 'lucide-react';
import ThemeToggle from '../components/ThemeToggle';

export function Login() {
  const { authMode, login, verifyOTP } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [otpCode, setOtpCode] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [step, setStep] = useState<'credentials' | 'otp'>('credentials');
  const [loading, setLoading] = useState(false);
  // An SSO sign-in that came back refused lands here with its reason.
  const [error, setError] = useState<string>(() => (location.state as { ssoError?: string } | null)?.ssoError || '');
  const [ssoProviders, setSsoProviders] = useState<SsoProvider[]>([]);
  const [redirectingTo, setRedirectingTo] = useState<string | null>(null);
  // The provider the typed email's domain must sign in with, once looked up.
  const [enforced, setEnforced] = useState<SsoProvider | null>(null);
  const emailRef = useRef(email);
  emailRef.current = email;

  const requiresPassword = authMode === 'password' || authMode === 'password+otp';

  useEffect(() => {
    apiClient.listSsoProviders()
      .then(setSsoProviders)
      .catch(() => setSsoProviders([]));  // an older backend: no SSO
  }, []);

  const lookUpEnforced = async () => {
    if (ssoProviders.length === 0 || !email.includes('@')) return;
    const asked = email;
    const found = await apiClient.discoverSsoProvider(asked).catch(() => null);
    // Only for the address still in the box (it may have changed meanwhile).
    if (emailRef.current === asked) setEnforced(found);
  };

  const signInWith = async (provider: SsoProvider) => {
    setError('');
    setRedirectingTo(provider.display_name);
    try {
      await startSso(provider.name, returnPath(location.state));
    } catch (err: any) {
      setRedirectingTo(null);
      setError(err.message || 'Single sign-on could not start');
    }
  };

  const handleCredentialsSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      // A domain that must use single sign-on goes straight to its provider.
      const sso = enforced ?? (ssoProviders.length > 0 ? await apiClient.discoverSsoProvider(email).catch(() => null) : null);
      if (sso) {
        await signInWith(sso);
        return;
      }
      const newSessionId = await login(email, requiresPassword ? password : undefined);
      if (newSessionId) {
        // OTP step follows
        setSessionId(newSessionId);
        setStep('otp');
      } else {
        // password-only mode: tokens are already in storage
        navigate(returnPath(location.state));
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Login failed');
    } finally {
      setLoading(false);
    }
  };

  const handleOTPSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      await verifyOTP(sessionId, otpCode);
      navigate(returnPath(location.state));
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Invalid OTP code');
    } finally {
      setLoading(false);
    }
  };

  const credentialsCta = (() => {
    if (enforced) return `Continue with ${enforced.display_name}`;
    if (loading) return 'Signing in...';
    if (authMode === 'otp') return 'Continue with email';
    if (authMode === 'password') return 'Sign in';
    return 'Continue';
  })();

  const credentialsHelp = (() => {
    if (authMode === 'otp') return 'Enter your email to receive a login code';
    if (authMode === 'password') return 'Sign in with your email and password';
    return 'Sign in with your password — a verification code follows';
  })();

  return (
    <div className="min-h-screen bg-surface-page flex items-center justify-center p-4">
      <div className="absolute top-4 right-4">
        <ThemeToggle />
      </div>
      <div className="w-full max-w-md">
        {/* Logo and title */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center mb-4">
            <Logo className="h-12 w-auto" />
          </div>
          <h1 className="text-xl font-semibold text-gray-100 mb-2">Management Console</h1>
          <p className="text-gray-400">Sovereign Infrastructure for Native Agentic Systems</p>
        </div>

        {/* Login card */}
        <div className="bg-surface-1 rounded-2xl p-8 border border-line-soft">
          {step === 'credentials' ? (
            <>
              <div className="mb-6">
                <h2 className="text-2xl font-semibold text-gray-100 mb-2">Welcome back</h2>
                <p className="text-gray-400">{credentialsHelp}</p>
              </div>

              {ssoProviders.length > 0 && (
                <div className="space-y-2 mb-6">
                  {ssoProviders.map((provider) => (
                    <button
                      key={provider.name}
                      type="button"
                      onClick={() => signInWith(provider)}
                      disabled={redirectingTo !== null}
                      className="w-full btn btn-secondary py-3 rounded-lg flex items-center justify-center disabled:opacity-50"
                    >
                      {redirectingTo === provider.display_name ? (
                        <Loader2 className="w-5 h-5 mr-2 animate-spin" />
                      ) : (
                        <LogIn className="w-5 h-5 mr-2" />
                      )}
                      Sign in with {provider.display_name}
                    </button>
                  ))}
                  <div className="flex items-center gap-3 pt-2 text-xs text-gray-500">
                    <div className="flex-1 border-t border-line-soft" />
                    or
                    <div className="flex-1 border-t border-line-soft" />
                  </div>
                </div>
              )}

              <form onSubmit={handleCredentialsSubmit} className="space-y-4">
                <div>
                  <label htmlFor="email" className="block text-sm font-medium text-gray-300 mb-2">
                    Email address
                  </label>
                  <div className="relative">
                    <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-gray-500" />
                    <input
                      id="email"
                      type="email"
                      value={email}
                      onChange={(e) => {
                        setEmail(e.target.value);
                        setEnforced(null);
                      }}
                      onBlur={lookUpEnforced}
                      placeholder="you@example.com"
                      autoComplete="email"
                      required
                      className="w-full pl-10 pr-4 py-3 bg-surface-input border border-line rounded-lg text-gray-100 placeholder-gray-600 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                    />
                  </div>
                </div>

                {enforced && (
                  <p className="text-sm text-gray-400">
                    Accounts at {email.split('@')[1]} sign in with {enforced.display_name}.
                  </p>
                )}

                {requiresPassword && !enforced && (
                  <div>
                    <label htmlFor="password" className="block text-sm font-medium text-gray-300 mb-2">
                      Password
                    </label>
                    <div className="relative">
                      <KeyRound className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-gray-500" />
                      <input
                        id="password"
                        type="password"
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                        placeholder="••••••••"
                        autoComplete="current-password"
                        required
                        className="w-full pl-10 pr-4 py-3 bg-surface-input border border-line rounded-lg text-gray-100 placeholder-gray-600 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                      />
                    </div>
                  </div>
                )}

                {error && (
                  <div className="p-3 bg-red-900/20 border border-red-800/30 rounded-lg text-sm text-red-400">
                    {error}
                  </div>
                )}

                {redirectingTo && (
                  <p className="text-sm text-gray-400">Taking you to {redirectingTo}...</p>
                )}

                <button
                  type="submit"
                  disabled={loading || redirectingTo !== null || !email || (requiresPassword && !enforced && !password)}
                  className="w-full btn btn-primary py-3 rounded-lg flex items-center justify-center disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {loading ? (
                    <>
                      <Loader2 className="w-5 h-5 mr-2 animate-spin" />
                      {credentialsCta}
                    </>
                  ) : (
                    credentialsCta
                  )}
                </button>

                {requiresPassword && (
                  <div className="text-center">
                    <Link to="/reset-password" className="text-sm text-gray-400 hover:text-gray-200">
                      Have a reset link? Set a new password
                    </Link>
                  </div>
                )}
              </form>
            </>
          ) : (
            <>
              <div className="mb-6">
                <h2 className="text-2xl font-semibold text-gray-100 mb-2">
                  {requiresPassword ? 'One more step' : 'Verify your email'}
                </h2>
                <p className="text-gray-400">
                  We sent a code to <span className="font-medium text-gray-100">{email}</span>
                </p>
              </div>

              <form onSubmit={handleOTPSubmit} className="space-y-4">
                <div>
                  <label htmlFor="otp" className="block text-sm font-medium text-gray-300 mb-2">
                    Verification code
                  </label>
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
                      required
                      maxLength={6}
                      className="w-full pl-10 pr-4 py-3 bg-surface-input border border-line rounded-lg text-gray-100 placeholder-gray-600 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent text-center text-2xl font-mono tracking-widest"
                    />
                  </div>
                </div>

                {error && (
                  <div className="p-3 bg-red-900/20 border border-red-800/30 rounded-lg text-sm text-red-400">
                    {error}
                  </div>
                )}

                <button
                  type="submit"
                  disabled={loading || otpCode.length !== 6}
                  className="w-full btn btn-primary py-3 rounded-lg flex items-center justify-center disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {loading ? (
                    <>
                      <Loader2 className="w-5 h-5 mr-2 animate-spin" />
                      Verifying...
                    </>
                  ) : (
                    'Verify and continue'
                  )}
                </button>

                <button
                  type="button"
                  onClick={() => {
                    setStep('credentials');
                    setOtpCode('');
                    setError('');
                  }}
                  className="w-full text-sm text-gray-400 hover:text-gray-200"
                >
                  {requiresPassword ? 'Use a different account' : 'Use a different email'}
                </button>
              </form>
            </>
          )}
        </div>

        <p className="text-center text-sm text-gray-500 mt-6">
          Secure authentication powered by Sinas
        </p>
      </div>
    </div>
  );
}
