import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { startAuthentication, startRegistration } from '@simplewebauthn/browser';
import { api, setCsrf } from './api.js';
import { Blueprint, Button } from './components.js';

/**
 * Sign in.
 *
 * A passkey when one is enrolled, the recovery password otherwise — and after a
 * recovery login the user is invited to enrol a passkey immediately, so the
 * password is a way back in rather than the daily route.
 */
export function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [enrolling, setEnrolling] = useState(false);

  const state = useQuery({
    queryKey: ['auth-state'],
    queryFn: () => api.get<{ seeded: boolean; has_passkey: boolean }>('/api/auth/state'),
  });

  const withPasskey = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      const options = await api.post<Parameters<typeof startAuthentication>[0]['optionsJSON']>(
        '/api/auth/login/options',
      );
      const assertion = await startAuthentication({ optionsJSON: options });
      const res = await api.post<{ csrf: string }>('/api/auth/login/verify', assertion);
      setCsrf(res.csrf);
      onSignedIn();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'sign-in failed');
    } finally {
      setBusy(false);
    }
  };

  const withPassword = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.post<{ csrf: string; enroll_passkey?: boolean }>('/api/auth/recover', {
        password,
      });
      setCsrf(res.csrf);
      setPassword('');
      if (res.enroll_passkey) setEnrolling(true);
      else onSignedIn();
    } catch {
      setError('That password does not match.');
    } finally {
      setBusy(false);
    }
  };

  const enrolPasskey = async (): Promise<void> => {
    setBusy(true);
    try {
      const options = await api.post<Parameters<typeof startRegistration>[0]['optionsJSON']>(
        '/api/auth/register/options',
      );
      const attestation = await startRegistration({ optionsJSON: options });
      await api.post('/api/auth/register/verify', attestation);
    } catch {
      // Enrolment is an improvement, not a gate: a failure here still leaves a
      // signed-in session behind it.
    } finally {
      setBusy(false);
      onSignedIn();
    }
  };

  if (enrolling) {
    return (
      <Shell>
        <h1 className="headline" style={{ fontSize: 40 }}>
          Add a passkey
        </h1>
        <p className="muted-70" style={{ fontSize: 14, lineHeight: 1.6, maxWidth: '38ch' }}>
          You are signed in. Adding a passkey now means you will not need the recovery password
          again — and there is no e-mail to intercept, because Loop never sends one.
        </p>
        <div style={{ display: 'grid', gap: 'var(--space-3)', marginTop: 'var(--space-6)' }}>
          <Button variant="primary" onClick={() => void enrolPasskey()} disabled={busy}>
            Add a passkey
          </Button>
          <Button onClick={onSignedIn}>Not now</Button>
        </div>
      </Shell>
    );
  }

  if (state.data && !state.data.seeded) {
    return (
      <Shell>
        <h1 className="headline" style={{ fontSize: 34 }}>
          No account yet
        </h1>
        <p className="muted-70" style={{ fontSize: 14, lineHeight: 1.6, maxWidth: '42ch' }}>
          This box has no user. Create one from a shell on the machine:
        </p>
        <pre
          style={{
            border: '1px solid var(--color-divider)',
            padding: 'var(--space-4)',
            fontSize: 12.5,
            overflowX: 'auto',
          }}
        >
          npm run seed:user -- you@example.com
        </pre>
      </Shell>
    );
  }

  return (
    <Shell>
      <div className="eyebrow">Loop</div>
      <h1 className="headline" style={{ fontSize: 40, margin: 'var(--space-3) 0 var(--space-6)' }}>
        Sign in
      </h1>

      {state.data?.has_passkey ? (
        <Button variant="primary" onClick={() => void withPasskey()} disabled={busy} style={{ width: '100%' }}>
          Use your passkey
        </Button>
      ) : null}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void withPassword();
        }}
        style={{ marginTop: 'var(--space-6)', display: 'grid', gap: 'var(--space-3)' }}
      >
        <label className="eyebrow" htmlFor="recovery">
          {state.data?.has_passkey ? 'or the recovery password' : 'recovery password'}
        </label>
        <input
          id="recovery"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          style={{
            border: '1px solid var(--color-divider)',
            background: 'transparent',
            padding: 'var(--space-3)',
            minHeight: 44,
            font: 'inherit',
            color: 'inherit',
          }}
        />
        <Button variant={state.data?.has_passkey ? 'secondary' : 'primary'} type="submit" disabled={busy || password.length < 8}>
          Sign in
        </Button>
      </form>

      {error ? (
        <p style={{ color: 'var(--color-accent-800)', fontSize: 13, marginTop: 'var(--space-4)' }}>{error}</p>
      ) : null}
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ display: 'grid', placeItems: 'center', height: '100%', padding: 18 }}>
      <Blueprint style={{ padding: 'var(--space-8)', width: 'min(420px, 100%)' }}>{children}</Blueprint>
    </div>
  );
}
