import { createHash, createHmac, randomBytes, scrypt as scryptCb, timingSafeEqual } from 'node:crypto';
import { promisify } from 'node:util';
import type pg from 'pg';

const scrypt = promisify(scryptCb) as (
  password: string | Buffer,
  salt: string | Buffer,
  keylen: number,
  options: { N: number; r: number; p: number; maxmem: number },
) => Promise<Buffer>;

/**
 * Authentication.
 *
 * The Engineering Spec assumes a cookie session (§13) and puts sign-up in P4,
 * so the single-tenant mechanism was never specified. It is a passkey
 * (WebAuthn) with a recovery password: no shared secret to phish, and — the
 * part that matters here — no email to send, so §12's "there is no send path
 * anywhere in the repo" stays absolute. decisions.md OPEN-4.
 *
 * The recovery password is hashed with scrypt from node's own crypto rather
 * than argon2id: it is the only place a password appears, the parameters below
 * exceed the OWASP scrypt floor, and it avoids a native addon on an ARM box.
 */

const SCRYPT = { N: 2 ** 16, r: 8, p: 2, maxmem: 256 * 1024 * 1024 } as const;
const SESSION_TTL_DAYS = 30;

export async function hashPassword(password: string): Promise<string> {
  const salt = randomBytes(16);
  const derived = await scrypt(password.normalize('NFKC'), salt, 64, SCRYPT);
  return `scrypt$${SCRYPT.N}$${SCRYPT.r}$${SCRYPT.p}$${salt.toString('base64')}$${derived.toString('base64')}`;
}

export async function verifyPassword(password: string, stored: string): Promise<boolean> {
  const [scheme, n, r, p, saltB64, hashB64] = stored.split('$');
  if (scheme !== 'scrypt' || !saltB64 || !hashB64) return false;
  const derived = await scrypt(password.normalize('NFKC'), Buffer.from(saltB64, 'base64'), 64, {
    N: Number(n),
    r: Number(r),
    p: Number(p),
    maxmem: SCRYPT.maxmem,
  });
  const expected = Buffer.from(hashB64, 'base64');
  return derived.length === expected.length && timingSafeEqual(derived, expected);
}

export interface Session {
  id: string;
  userId: string;
  csrf: string;
}

const sha256 = (v: string): Buffer => createHash('sha256').update(v).digest();

export interface IssuedSession {
  token: string;
  csrf: string;
  expiresAt: Date;
}

/**
 * The CSRF token is *derived*, not stored.
 *
 * It used to be a random value whose hash went into the session row — which
 * works exactly once, at login, because a hash cannot be turned back into the
 * token. On the next page load `/api/me` had nothing to hand back but the hash
 * itself, the client presented that, the server hashed it a second time, and
 * every mutation failed with 403. The symptom was a button that did nothing.
 *
 * An HMAC over the session id fixes it without storing a live secret: it is
 * recomputable on any request, identical for the whole life of the session,
 * and useless to anyone who does not also hold the session cookie.
 */
export function csrfFor(sessionId: string): string {
  const secret = process.env.SESSION_SECRET;
  if (!secret) throw new Error('SESSION_SECRET is not set');
  return createHmac('sha256', secret).update(`csrf:${sessionId}`).digest('base64url');
}

/**
 * Only the session token's hash is stored. A database dump does not hand over
 * live sessions, which is the same reasoning that keeps mailbox tokens sealed.
 */
export async function createSession(sql: pg.Pool | pg.PoolClient, userId: string): Promise<IssuedSession> {
  const token = randomBytes(32).toString('base64url');
  const expiresAt = new Date(Date.now() + SESSION_TTL_DAYS * 86_400_000);
  const res = await sql.query<{ id: string }>(
    `insert into sessions (user_id, token_hash, expires_at) values ($1,$2,$3) returning id`,
    [userId, sha256(token), expiresAt],
  );
  return { token, csrf: csrfFor(res.rows[0]!.id), expiresAt };
}

export async function loadSession(
  sql: pg.Pool | pg.PoolClient,
  token: string | undefined,
): Promise<Session | null> {
  if (!token) return null;
  const res = await sql.query<{ id: string; user_id: string }>(
    `update sessions set last_seen_at = now()
      where token_hash = $1 and expires_at > now()
      returning id, user_id`,
    [sha256(token)],
  );
  const row = res.rows[0];
  if (!row) return null;
  return { id: row.id, userId: row.user_id, csrf: csrfFor(row.id) };
}

export async function destroySession(sql: pg.Pool | pg.PoolClient, token: string): Promise<void> {
  await sql.query(`delete from sessions where token_hash = $1`, [sha256(token)]);
}

/** Constant-time comparison of the submitted CSRF token against the session. */
export function csrfMatches(session: Session, presented: string | undefined): boolean {
  if (!presented) return false;
  const a = Buffer.from(presented);
  const b = Buffer.from(session.csrf);
  return a.length === b.length && timingSafeEqual(a, b);
}

export const SESSION_COOKIE = 'loop_session';

export function sessionCookieOptions(secure: boolean) {
  return {
    httpOnly: true,
    sameSite: 'lax' as const,
    secure,
    path: '/',
    maxAge: SESSION_TTL_DAYS * 86_400,
  };
}
