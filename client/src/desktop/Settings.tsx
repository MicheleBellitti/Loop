import { useEffect, useRef, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { api, type MailboxHealth } from '../api.js';
import { Button } from '../components.js';

/**
 * The account, and the way into it.
 *
 * Settings used to be the fourth item in the top navigation, beside Pipeline,
 * Statistics and Review — which put "where is my data going" on the same footing
 * as the two screens the product is for. It is not a section, it is the account
 * behind them, so it hangs off an avatar in the corner where every application
 * of this shape has put it for a decade and where people look for it first.
 */

const AVATAR_KEY = 'loop.avatar';
const NAME_KEY = 'loop.display_name';
/** A data URL in localStorage. Anything larger is a photo, not an avatar. */
const MAX_AVATAR_BYTES = 256 * 1024;

export function displayName(email: string | undefined): string {
  const stored = localStorage.getItem(NAME_KEY);
  if (stored) return stored;
  const local = (email ?? '').split('@')[0] ?? '';
  return local || 'You';
}

/**
 * Two letters, from a name or from an address.
 *
 * `michele bellitti` → MB; `michelebellitti78` → MI. Digits are dropped rather
 * than shown, because "M7" is nobody's initials.
 */
export function initialsOf(name: string): string {
  const words = name.replace(/[^\p{L}\p{N}\s._-]/gu, ' ').split(/[\s._-]+/).filter(Boolean);
  const letters = words.map((w) => [...w].find((c) => /\p{L}/u.test(c)) ?? '').filter(Boolean);
  if (letters.length >= 2) return (letters[0]! + letters[1]!).toUpperCase();
  const first = words[0] ?? '';
  const onlyLetters = [...first].filter((c) => /\p{L}/u.test(c));
  return (onlyLetters.slice(0, 2).join('') || '?').toUpperCase();
}

export function storedAvatar(): string | null {
  return localStorage.getItem(AVATAR_KEY);
}

export function Avatar({ email, size = 34 }: { email: string | undefined; size?: number }) {
  const name = displayName(email);
  const src = storedAvatar();
  return src ? (
    <img className="avatar" src={src} alt="" width={size} height={size} style={{ width: size, height: size }} />
  ) : (
    <span className="avatar" style={{ width: size, height: size, fontSize: size * 0.36 }} aria-hidden="true">
      {initialsOf(name)}
    </span>
  );
}

export function ProfileMenu({
  email,
  current,
  onSettings,
}: {
  email: string | undefined;
  current: boolean;
  onSettings: () => void;
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent): void => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    };
    const key = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', away);
    document.addEventListener('keydown', key);
    return () => {
      document.removeEventListener('mousedown', away);
      document.removeEventListener('keydown', key);
    };
  }, [open]);

  const signOut = useMutation({
    mutationFn: () => api.post('/api/auth/logout'),
    // A reload rather than a state change: every cached query belongs to the
    // session that has just ended.
    onSettled: () => window.location.reload(),
  });

  return (
    <div className="profile" ref={wrap}>
      <button
        className="profile-button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-current={current ? 'page' : undefined}
        onClick={() => setOpen((v) => !v)}
        title={email ?? 'Your account'}
      >
        <Avatar email={email} />
      </button>

      {open ? (
        <div className="profile-menu" role="menu">
          <div className="profile-menu-head">
            <Avatar email={email} size={40} />
            <div style={{ minWidth: 0 }}>
              <div style={{ font: '600 14px var(--font-heading)' }}>{displayName(email)}</div>
              <div className="muted-60" style={{ fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {email ?? '—'}
              </div>
            </div>
          </div>
          <button
            role="menuitem"
            onClick={() => {
              setOpen(false);
              onSettings();
            }}
          >
            Settings
          </button>
          <a role="menuitem" href="/api/export?format=json">
            Export your data
          </a>
          <button role="menuitem" disabled={signOut.isPending} onClick={() => signOut.mutate()}>
            {signOut.isPending ? 'Signing out…' : 'Sign out'}
          </button>
        </div>
      ) : null}
    </div>
  );
}

/**
 * Settings.
 *
 * Deliberately small: who you are, what is connected, how to get your data out,
 * and how to destroy it. The two GDPR endpoints are the ones §15 says must be
 * reachable "by construction, not by a support inbox", so they are buttons
 * rather than documentation.
 */
export function SettingsView({ health, email }: { health: MailboxHealth | undefined; email?: string }) {
  const reconnect = useMutation({
    mutationFn: () => api.post<{ url: string }>('/api/mailboxes/gmail/start'),
    onSuccess: (res) => {
      window.location.href = res.url;
    },
  });

  return (
    <div className="desk-single" style={{ maxWidth: 720, paddingTop: 'var(--space-6)' }}>
      <Profile email={email} />

      <section style={{ marginBottom: 'var(--space-8)' }}>
        <div className="section-label" style={{ marginBottom: 'var(--space-3)' }}>Mailboxes</div>
        <div style={{ border: '1px solid var(--color-divider)' }}>
          {(health?.providers ?? []).map((p) => (
            <div
              key={p.id}
              style={{ padding: 'var(--space-4)', borderBottom: '1px solid var(--color-divider)', display: 'flex', justifyContent: 'space-between' }}
            >
              {/* Keyed on the row and named by its address: two Gmail accounts
                  are two mailboxes, and "Gmail / Gmail" tells you nothing. */}
              <span>
                {p.provider === 'gmail' ? 'Gmail' : 'Google Calendar'}
                <span className="muted-65"> · {p.address}</span>
              </span>
              <span className={p.status === 'ok' ? 'emphasis' : 'muted-65'}>{p.status}</span>
            </div>
          ))}
          {!health?.providers.length ? (
            <div style={{ padding: 'var(--space-4)' }} className="muted-65">No mailbox connected.</div>
          ) : null}
        </div>
        <div style={{ marginTop: 'var(--space-3)' }}>
          <Button onClick={() => reconnect.mutate()} disabled={reconnect.isPending}>
            {health?.connected ? 'Reconnect Google' : 'Connect a mailbox'}
          </Button>
        </div>
      </section>

      <section style={{ marginBottom: 'var(--space-8)' }}>
        <div className="section-label" style={{ marginBottom: 'var(--space-3)' }}>Your data</div>
        <p className="muted-70" style={{ fontSize: 13, marginBottom: 'var(--space-3)' }}>
          The complete event log and every application, machine-readable, no rate limit. This is
          Article 15 and Article 20 satisfied by an endpoint rather than by a request.
        </p>
        <div style={{ display: 'flex', gap: 'var(--space-3)' }}>
          <a className="btn" href="/api/export?format=json">Export JSON</a>
          <a className="btn" href="/api/export?format=csv">Export CSV</a>
        </div>
      </section>

      <section>
        <div className="section-label" style={{ marginBottom: 'var(--space-3)' }}>Delete everything</div>
        <p className="muted-70" style={{ fontSize: 13, marginBottom: 'var(--space-3)' }}>
          A real cascade: applications, the event log, the queues, the projections, the vector
          index, push subscriptions, and the OAuth grant at Google. It returns a receipt id and it
          cannot be undone.
        </p>
        <a className="btn" href="/settings/delete">Delete my account…</a>
      </section>
    </div>
  );
}

/**
 * The name and the picture.
 *
 * Both live in this browser's local storage and nowhere else. There is no
 * column for them and no upload route, and adding one would mean a product that
 * reads your mailbox also holding a photograph of you on a server — for a
 * decoration. So the trade is stated rather than hidden: it is per-device, and
 * it never leaves the device.
 */
function Profile({ email }: { email?: string }) {
  const [name, setName] = useState(() => displayName(email));
  const [avatar, setAvatar] = useState<string | null>(() => storedAvatar());
  const [error, setError] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);

  const save = (next: string): void => {
    setName(next);
    if (next.trim()) localStorage.setItem(NAME_KEY, next.trim());
    else localStorage.removeItem(NAME_KEY);
  };

  const pick = (chosen: File | undefined): void => {
    if (!chosen) return;
    if (!chosen.type.startsWith('image/')) return setError('That is not an image.');
    if (chosen.size > MAX_AVATAR_BYTES) return setError('Pick something under 256 kB.');
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result);
      localStorage.setItem(AVATAR_KEY, url);
      setAvatar(url);
      setError(null);
    };
    reader.readAsDataURL(chosen);
  };

  return (
    <section style={{ marginBottom: 'var(--space-8)' }}>
      <div className="section-label" style={{ marginBottom: 'var(--space-3)' }}>You</div>
      <div style={{ display: 'flex', gap: 'var(--space-4)', alignItems: 'center' }}>
        {avatar ? (
          <img className="avatar" src={avatar} alt="" style={{ width: 64, height: 64 }} />
        ) : (
          <span className="avatar" style={{ width: 64, height: 64, fontSize: 23 }} aria-hidden="true">
            {initialsOf(name)}
          </span>
        )}
        <div style={{ display: 'grid', gap: 'var(--space-2)', flex: 1 }}>
          <label className="eyebrow" htmlFor="display-name">Display name</label>
          <input
            id="display-name"
            className="field"
            value={name}
            onChange={(e) => save(e.target.value)}
            placeholder={email ?? 'You'}
          />
          <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
            <input
              ref={file}
              type="file"
              accept="image/*"
              hidden
              onChange={(e) => pick(e.target.files?.[0])}
            />
            <Button onClick={() => file.current?.click()}>Upload a photo</Button>
            {avatar ? (
              <Button
                onClick={() => {
                  localStorage.removeItem(AVATAR_KEY);
                  setAvatar(null);
                }}
              >
                Use initials
              </Button>
            ) : null}
          </div>
        </div>
      </div>
      <p className="muted-50" style={{ fontSize: 11.5, lineHeight: 1.5, marginTop: 'var(--space-3)' }}>
        {error ? `${error} ` : ''}
        Your name and photo are kept in this browser only — they are never uploaded, and no other
        device will see them. Signed in as {email ?? 'unknown'}.
      </p>
    </section>
  );
}
