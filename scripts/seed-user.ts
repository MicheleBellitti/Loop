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

const email = process.argv[2];
if (!email || !email.includes('@')) {
  console.error('usage: npm run seed:user -- you@example.com');
  process.exit(1);
}

const tz = process.argv[3] ?? Intl.DateTimeFormat().resolvedOptions().timeZone ?? 'Europe/Rome';

const pool = createPool({ applicationName: 'loop-seed' });

const existing = await pool.query<{ id: string }>('select id from users limit 1');
if (existing.rowCount) {
  console.error(
    'This box already has a user. Loop is single-tenant by design; opening it to a second\n' +
      'person is phase 4, which is a different product with a different burden.',
  );
  await pool.end();
  process.exit(1);
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
