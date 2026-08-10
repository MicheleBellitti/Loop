import type { FastifyInstance } from 'fastify';
import type pg from 'pg';
import { z } from 'zod';
import {
  generateAuthenticationOptions,
  generateRegistrationOptions,
  verifyAuthenticationResponse,
  verifyRegistrationResponse,
} from '@simplewebauthn/server';
import type { Config, Logger } from '@loop/runtime';
import {
  createSession,
  destroySession,
  loadSession,
  SESSION_COOKIE,
  sessionCookieOptions,
  verifyPassword,
} from './auth.js';

/**
 * Passkey registration and login, with a recovery password.
 *
 * There is exactly one user on a single-tenant box, seeded by
 * `npm run seed:user`. The first login uses the recovery password shown once at
 * seed time; a passkey is enrolled immediately afterwards and is the normal
 * path from then on. No email is involved anywhere, which is what keeps §12's
 * "no send path in the repo" absolute. decisions.md OPEN-4.
 */

interface Deps {
  pool: pg.Pool;
  config: Config;
  log: Logger;
}

const CHALLENGE_TTL_MS = 5 * 60_000;

export async function registerAuthRoutes(app: FastifyInstance, deps: Deps): Promise<void> {
  const { pool, config } = deps;
  const rpID = config.webauthn.rpId;
  const origin = config.publicOrigin;
  const secure = origin.startsWith('https:');

  const soleUser = async (): Promise<{ id: string; email: string } | null> => {
    const res = await pool.query<{ id: string; email: string }>(
      `select id, email from users order by created_at limit 1`,
    );
    return res.rows[0] ?? null;
  };

  const saveChallenge = async (userId: string, challenge: string): Promise<void> => {
    await pool.query(
      `insert into auth_secrets (user_id, recovery_hash, webauthn_challenge, challenge_expires_at)
       values ($1, '', $2, now() + interval '5 minutes')
       on conflict (user_id) do update
         set webauthn_challenge = excluded.webauthn_challenge,
             challenge_expires_at = excluded.challenge_expires_at`,
      [userId, challenge],
    );
  };

  const takeChallenge = async (userId: string): Promise<string | null> => {
    const res = await pool.query<{ webauthn_challenge: string | null }>(
      `update auth_secrets set webauthn_challenge = null
        where user_id = $1 and challenge_expires_at > now()
        returning webauthn_challenge`,
      [userId],
    );
    return res.rows[0]?.webauthn_challenge ?? null;
  };

  app.get('/api/auth/state', async () => {
    const user = await soleUser();
    if (!user) return { seeded: false, has_passkey: false };
    const creds = await pool.query<{ n: string }>(
      `select count(*)::text as n from credentials where user_id = $1`,
      [user.id],
    );
    return { seeded: true, has_passkey: Number(creds.rows[0]?.n ?? '0') > 0 };
  });

  // ── login ────────────────────────────────────────────────────────────────
  app.post(
    '/api/auth/login/options',
    { config: { rateLimit: { max: 20, timeWindow: '1 minute' } } },
    async (_req, reply) => {
      const user = await soleUser();
      if (!user) return reply.code(404).send({ error: { code: 'not_seeded', message: 'run npm run seed:user' } });

      const creds = await pool.query<{ credential_id: string; transports: string[] }>(
        `select credential_id, transports from credentials where user_id = $1`,
        [user.id],
      );
      const options = await generateAuthenticationOptions({
        rpID,
        userVerification: 'required',
        allowCredentials: creds.rows.map((c) => ({
          id: c.credential_id,
          transports: c.transports as never,
        })),
      });
      await saveChallenge(user.id, options.challenge);
      return options;
    },
  );

  app.post(
    '/api/auth/login/verify',
    { config: { rateLimit: { max: 20, timeWindow: '1 minute' } } },
    async (req, reply) => {
      const user = await soleUser();
      if (!user) return reply.code(404).send({ error: { code: 'not_seeded', message: 'no user' } });
      const challenge = await takeChallenge(user.id);
      if (!challenge) return reply.code(400).send({ error: { code: 'no_challenge', message: 'request options first' } });

      const body = req.body as Parameters<typeof verifyAuthenticationResponse>[0]['response'];
      const credentialId = (body as { id?: string }).id;
      const cred = await pool.query<{ credential_id: string; public_key: Buffer; counter: string }>(
        `select credential_id, public_key, counter::text from credentials
          where user_id = $1 and credential_id = $2`,
        [user.id, credentialId],
      );
      if (!cred.rows[0]) return reply.code(401).send({ error: { code: 'unknown_credential', message: 'unknown passkey' } });

      let verification;
      try {
        verification = await verifyAuthenticationResponse({
          response: body,
          expectedChallenge: challenge,
          expectedOrigin: origin,
          expectedRPID: rpID,
          credential: {
            id: cred.rows[0].credential_id,
            publicKey: new Uint8Array(cred.rows[0].public_key),
            counter: Number(cred.rows[0].counter),
          },
          requireUserVerification: true,
        });
      } catch (err) {
        deps.log.warn({ msg: 'passkey verification failed', error: (err as Error).message });
        return reply.code(401).send({ error: { code: 'bad_assertion', message: 'verification failed' } });
      }

      if (!verification.verified) {
        return reply.code(401).send({ error: { code: 'bad_assertion', message: 'verification failed' } });
      }

      // A counter that goes backwards is the classic cloned-authenticator
      // signal. Stored authenticators that report 0 always are exempt.
      await pool.query(
        `update credentials set counter = $3, last_used_at = now()
          where user_id = $1 and credential_id = $2`,
        [user.id, credentialId, verification.authenticationInfo.newCounter],
      );

      const session = await createSession(pool, user.id);
      reply.setCookie(SESSION_COOKIE, session.token, sessionCookieOptions(secure));
      return { ok: true, csrf: session.csrf };
    },
  );

  // ── recovery password ────────────────────────────────────────────────────
  app.post(
    '/api/auth/recover',
    { config: { rateLimit: { max: 5, timeWindow: '15 minutes' } } },
    async (req, reply) => {
      const parsed = z.object({ password: z.string().min(8) }).safeParse(req.body);
      if (!parsed.success) return reply.code(400).send({ error: { code: 'bad_body', message: 'password required' } });

      const user = await soleUser();
      if (!user) return reply.code(404).send({ error: { code: 'not_seeded', message: 'no user' } });

      const secret = await pool.query<{ recovery_hash: string }>(
        `select recovery_hash from auth_secrets where user_id = $1`,
        [user.id],
      );
      const hash = secret.rows[0]?.recovery_hash;
      if (!hash || !(await verifyPassword(parsed.data.password, hash))) {
        deps.log.warn({ msg: 'recovery attempt failed' });
        return reply.code(401).send({ error: { code: 'bad_password', message: 'wrong password' } });
      }

      await pool.query(`update auth_secrets set recovery_used_at = now() where user_id = $1`, [user.id]);
      const session = await createSession(pool, user.id);
      reply.setCookie(SESSION_COOKIE, session.token, sessionCookieOptions(secure));
      return { ok: true, csrf: session.csrf, enroll_passkey: true };
    },
  );

  // ── passkey enrolment (session required) ─────────────────────────────────
  app.post('/api/auth/register/options', async (req, reply) => {
    const session = await loadSession(pool, req.cookies[SESSION_COOKIE]);
    if (!session) return reply.code(401).send({ error: { code: 'unauthenticated', message: 'sign in first' } });

    const user = await pool.query<{ email: string }>(`select email from users where id = $1`, [session.userId]);
    const existing = await pool.query<{ credential_id: string }>(
      `select credential_id from credentials where user_id = $1`,
      [session.userId],
    );

    const options = await generateRegistrationOptions({
      rpName: config.webauthn.rpName,
      rpID,
      userName: user.rows[0]?.email ?? 'loop',
      attestationType: 'none',
      excludeCredentials: existing.rows.map((c) => ({ id: c.credential_id })),
      authenticatorSelection: { residentKey: 'preferred', userVerification: 'required' },
    });
    await saveChallenge(session.userId, options.challenge);
    return options;
  });

  app.post('/api/auth/register/verify', async (req, reply) => {
    const session = await loadSession(pool, req.cookies[SESSION_COOKIE]);
    if (!session) return reply.code(401).send({ error: { code: 'unauthenticated', message: 'sign in first' } });

    const challenge = await takeChallenge(session.userId);
    if (!challenge) return reply.code(400).send({ error: { code: 'no_challenge', message: 'request options first' } });

    let verification;
    try {
      verification = await verifyRegistrationResponse({
        response: req.body as Parameters<typeof verifyRegistrationResponse>[0]['response'],
        expectedChallenge: challenge,
        expectedOrigin: origin,
        expectedRPID: rpID,
        requireUserVerification: true,
      });
    } catch (err) {
      return reply.code(400).send({ error: { code: 'bad_attestation', message: (err as Error).message } });
    }

    if (!verification.verified || !verification.registrationInfo) {
      return reply.code(400).send({ error: { code: 'bad_attestation', message: 'verification failed' } });
    }

    const { credential } = verification.registrationInfo;
    await pool.query(
      `insert into credentials (user_id, credential_id, public_key, counter, transports, label)
       values ($1,$2,$3,$4,$5,$6)
       on conflict (credential_id) do nothing`,
      [
        session.userId,
        credential.id,
        Buffer.from(credential.publicKey),
        credential.counter,
        credential.transports ?? [],
        'passkey',
      ],
    );
    return { ok: true };
  });

  app.post('/api/auth/logout', async (req, reply) => {
    const token = req.cookies[SESSION_COOKIE];
    if (token) await destroySession(pool, token);
    reply.clearCookie(SESSION_COOKIE, { path: '/' });
    return { ok: true };
  });

  app.get('/api/me', async (req, reply) => {
    const session = await loadSession(pool, req.cookies[SESSION_COOKIE]);
    if (!session) return reply.code(401).send({ error: { code: 'unauthenticated', message: 'sign in first' } });
    const res = await pool.query<{ email: string; tz: string; locale: string; display_currency: string }>(
      `select email, tz, locale, display_currency from users where id = $1`,
      [session.userId],
    );
    return { ...res.rows[0], csrf: session.csrf };
  });

  void CHALLENGE_TTL_MS;
}
