import { createHash, randomBytes } from 'node:crypto';
import type { FastifyInstance } from 'fastify';
import type pg from 'pg';
import { z } from 'zod';
import type { Config, Logger } from '@loop/runtime';
import { withUser, withUserReadOnly } from '@loop/db';
import { CONNECTOR } from '@loop/domain';
import { GoogleClient, storeMailbox } from '@loop/google';
import { loadSession, SESSION_COOKIE } from './auth.js';
import { mailboxHealth } from './queries.js';

/**
 * Connecting a mailbox, and the Pub/Sub webhook.
 *
 * The consent flow is the seven-step onboarding's whole purpose, so the
 * ordering matters: the scopes are named literally on screen before this
 * endpoint is ever called, and the only two requested here are `gmail.readonly`
 * and `calendar.readonly` — no send, no modify, no contacts.
 */

interface Deps {
  pool: pg.Pool;
  config: Config;
  log: Logger;
}

const SCOPE_VERSION = '2026-07-30.readonly';

export async function registerMailboxRoutes(app: FastifyInstance, deps: Deps): Promise<void> {
  const { pool, config, log } = deps;

  const google = new GoogleClient({
    clientId: config.google.clientId ?? '',
    clientSecret: config.google.clientSecret ?? '',
  });

  // PKCE verifiers live in memory only: they are single-use, short-lived, and
  // writing them to a table would be storing a secret for no reason.
  const pending = new Map<string, { verifier: string; userId: string; expires: number }>();
  setInterval(() => {
    const now = Date.now();
    for (const [k, v] of pending) if (v.expires < now) pending.delete(k);
  }, 60_000).unref();

  app.get('/api/mailboxes', async (req) =>
    withUserReadOnly(req.session!.userId, (sql) => mailboxHealth(sql, req.session!.userId), pool),
  );

  app.post('/api/mailboxes/gmail/start', async (req, reply) => {
    if (!config.google.clientId) {
      return reply.code(503).send({
        error: {
          code: 'google_not_configured',
          message: 'GOOGLE_CLIENT_ID is not set. See docs/google-setup.md.',
        },
      });
    }
    const verifier = randomBytes(32).toString('base64url');
    const challenge = createHash('sha256').update(verifier).digest('base64url');
    const state = randomBytes(16).toString('base64url');
    pending.set(state, { verifier, userId: req.session!.userId, expires: Date.now() + 10 * 60_000 });

    return {
      url: GoogleClient.authorizationUrl({
        clientId: config.google.clientId,
        redirectUri: config.google.redirectUri,
        codeChallenge: challenge,
        state,
      }),
      scopes: ['gmail.readonly', 'calendar.readonly'],
    };
  });

  app.get('/api/mailboxes/gmail/callback', async (req, reply) => {
    const q = z.object({ code: z.string(), state: z.string() }).safeParse(req.query);
    if (!q.success) return reply.code(400).send({ error: { code: 'bad_callback', message: 'missing code' } });

    const entry = pending.get(q.data.state);
    pending.delete(q.data.state);
    if (!entry || entry.expires < Date.now()) {
      return reply.code(400).send({ error: { code: 'stale_state', message: 'start the flow again' } });
    }

    const tokens = await google.exchangeCode(q.data.code, config.google.redirectUri, entry.verifier);
    const profile = await google.profile(tokens.access_token);

    await withUser(entry.userId, async (sql) => {
      const gmailId = await storeMailbox(sql, {
        userId: entry.userId,
        provider: 'gmail',
        address: profile.emailAddress,
        tokens,
      });
      await storeMailbox(sql, {
        userId: entry.userId,
        provider: 'google_calendar',
        address: profile.emailAddress,
        tokens,
      });
      // "Consent, captured at step 2 of onboarding with the scope list version
      // stored alongside the timestamp" — a row, not a promise.
      await sql.query(
        `insert into consents (user_id, kind, version, detail) values ($1,'mailbox_scopes',$2,$3)`,
        [entry.userId, SCOPE_VERSION, JSON.stringify({ scopes: tokens.scope.split(' ') })],
      );
      log.info({ user_id: entry.userId, mailbox_id: gmailId, msg: 'mailbox connected' });
    }, pool);

    return reply.redirect('/onboarding/scan');
  });

  const Backfill = z.object({ months: z.number().int().min(1).max(CONNECTOR.MAX_BACKFILL_MONTHS) });

  app.post('/api/mailboxes/backfill', async (req, reply) => {
    const parsed = Backfill.safeParse(req.body);
    if (!parsed.success) return reply.code(400).send({ error: { code: 'bad_body', message: parsed.error.issues[0]!.message } });

    await withUser(req.session!.userId, async (sql) => {
      const res = await sql.query<{ id: string }>(
        `select id from mailbox_accounts where user_id = $1 and provider = 'gmail' limit 1`,
        [req.session!.userId],
      );
      if (!res.rows[0]) throw Object.assign(new Error('no mailbox connected'), { statusCode: 409 });
      await sql.query('select pg_notify($1, $2)', [
        'loop_backfill',
        JSON.stringify({ mailbox_id: res.rows[0].id, months: parsed.data.months }),
      ]);
    }, pool);

    return { ok: true, months: parsed.data.months };
  });

  app.delete('/api/mailboxes/:id', async (req) => {
    const { id } = req.params as { id: string };
    await withUser(req.session!.userId, async (sql) => {
      await sql.query(`delete from mailbox_accounts where id = $1`, [id]);
    }, pool);
    return { ok: true };
  });

  /**
   * The Pub/Sub push endpoint.
   *
   * "Verify the JWT and the audience; it accepts no payload data." The body is
   * read only far enough to confirm it is well-formed and then discarded: the
   * connector re-reads history from its own stored cursor, so a forged
   * notification can at most cause an extra sync.
   */
  app.post('/api/gmail/push', { config: { rateLimit: { max: 600, timeWindow: '1 minute' } } }, async (req, reply) => {
    const authorization = req.headers.authorization;
    if (!authorization?.startsWith('Bearer ')) return reply.code(401).send();

    const ok = await verifyGoogleJwt(authorization.slice(7), config.publicOrigin);
    if (!ok) {
      log.warn({ msg: 'rejected an unsigned push notification' });
      return reply.code(401).send();
    }

    await pool.query('select pg_notify($1, $2)', ['loop_connector', 'push']);
    return reply.code(204).send();
  });

  app.get('/api/auth/session-check', async (req) => ({
    authenticated: !!(await loadSession(pool, req.cookies[SESSION_COOKIE])),
  }));
}

/**
 * Google-signed JWT verification against the published JWK set.
 *
 * Only the signature, the issuer, the expiry and the audience are checked —
 * which is everything that matters, because the claims themselves are ignored.
 */
type JsonWebKey = Record<string, unknown>;
interface Jwk { kid?: string; [k: string]: unknown }
let jwkCache: { keys: Jwk[]; fetchedAt: number } | null = null;

async function verifyGoogleJwt(token: string, expectedAudience: string): Promise<boolean> {
  const [headerB64, payloadB64, signatureB64] = token.split('.');
  if (!headerB64 || !payloadB64 || !signatureB64) return false;

  const decode = (s: string): Record<string, unknown> =>
    JSON.parse(Buffer.from(s, 'base64url').toString('utf8')) as Record<string, unknown>;

  let header: Record<string, unknown>;
  let payload: Record<string, unknown>;
  try {
    header = decode(headerB64);
    payload = decode(payloadB64);
  } catch {
    return false;
  }

  if (payload.iss !== 'https://accounts.google.com' && payload.iss !== 'accounts.google.com') return false;
  if (typeof payload.exp !== 'number' || payload.exp * 1000 < Date.now()) return false;
  if (typeof payload.aud === 'string' && !expectedAudience.includes(new URL(payload.aud).host ?? '')) {
    // An audience that names another deployment is not for us.
    if (payload.aud !== expectedAudience) return false;
  }

  if (!jwkCache || Date.now() - jwkCache.fetchedAt > 3_600_000) {
    const res = await fetch('https://www.googleapis.com/oauth2/v3/certs').catch(() => null);
    if (!res?.ok) return false;
    jwkCache = { keys: ((await res.json()) as { keys: Jwk[] }).keys, fetchedAt: Date.now() };
  }

  const jwk = jwkCache.keys.find((k) => k.kid === header.kid);
  if (!jwk) return false;

  const key = await crypto.subtle.importKey(
    'jwk',
    jwk as JsonWebKey,
    { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' },
    false,
    ['verify'],
  );
  return crypto.subtle.verify(
    'RSASSA-PKCS1-v1_5',
    key,
    Buffer.from(signatureB64, 'base64url'),
    Buffer.from(`${headerB64}.${payloadB64}`),
  );
}
