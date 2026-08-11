import { randomBytes } from 'node:crypto';
import { createPool } from '@loop/db';
import { hashPassword } from '../services/gateway/src/auth.js';

/**
 * `npm run seed:user -- you@example.com`
 *
 * Creates the single user on a single-tenant box and prints a recovery password
 * once. The passkey is enrolled from the browser at first sign-in; this password
 * exists so there is a way back in when the phone is lost, and it is shown here
 * rather than mailed because there is no send path in this repository.
 */

const args = process.argv.slice(2);
const reset = args.includes('--reset');
const positional = args.filter((a) => !a.startsWith('--'));
const email = positional[0];
if (!email || !email.includes('@')) {
  console.error('usage: npm run seed:user -- you@example.com [timezone]');
  console.error('       npm run seed:user -- you@example.com --reset');
  process.exit(1);
}

const tz = positional[1] ?? Intl.DateTimeFormat().resolvedOptions().timeZone ?? 'Europe/Rome';

const pool = createPool({ applicationName: 'loop-seed' });

const existing = await pool.query<{ id: string }>('select id from users limit 1');
if (existing.rowCount && !reset) {
  console.error(
    'This box already has a user. Loop is single-tenant by design; opening it to a second\n' +
      'person is phase 4, which is a different product with a different burden.\n\n' +
      'To issue a fresh recovery password for the existing user, pass --reset.',
  );
  await pool.end();
  process.exit(1);
}

/**
 * A recovery password is single-use by design, and a passkey can be lost with
 * the phone that held it. Without this the only way back into your own box was
 * to delete the user and every application with it — which is a data-loss
 * event dressed up as a password reset.
 *
 * Registered passkeys are deliberately left alone: this reissues the fallback,
 * it does not revoke the credentials that are still working.
 */
if (existing.rowCount && reset) {
  const row = await pool.query<{ id: string; email: string }>(
    'select id, email from users limit 1',
  );
  const target = row.rows[0]!;
  if (target.email.toLowerCase() !== email.toLowerCase()) {
    console.error(
      `This box belongs to ${target.email}, not ${email}. Refusing to reset a password for\n` +
        'an address that is not the one on record.',
    );
    await pool.end();
    process.exit(1);
  }
  const fresh = randomBytes(18).toString('base64url');
  await pool.query(
    `update auth_secrets
        set recovery_hash = $2, recovery_used_at = null
      where user_id = $1`,
    [target.id, await hashPassword(fresh)],
  );
  const passkeys = await pool.query<{ n: string }>(
    'select count(*)::text as n from credentials where user_id = $1',
    [target.id],
  );
  await pool.end();
  console.log(`
  recovery password reissued for ${target.email}

      ${fresh}

  Shown once. Registered passkeys left untouched (${passkeys.rows[0]!.n} on file).
`);
  process.exit(0);
}

// Long enough that it does not need to be memorable, short enough to type once.
const password = randomBytes(18).toString('base64url');

const user = await pool.query<{ id: string }>(
  `insert into users (email, tz) values ($1, $2) returning id`,
  [email, tz],
);
const userId = user.rows[0]!.id;

await pool.query('select seed_stage_defs($1)', [userId]);
await pool.query(
  `insert into auth_secrets (user_id, recovery_hash) values ($1, $2)
   on conflict (user_id) do update set recovery_hash = excluded.recovery_hash`,
  [userId, await hashPassword(password)],
);

await pool.end();

console.log(`
  user created

  email      ${email}
  timezone   ${tz}

  recovery password

      ${password}

  Write it down now — it is hashed, so this is the only time it is shown.
  Sign in with it once, add a passkey, and you will not need it again.
`);
