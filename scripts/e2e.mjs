#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';
import pg from 'pg';

/**
 * End to end, against the stub mailbox.
 *
 * Starts the five pipeline services for real, connects a mailbox pointed at the
 * fixture-replay server, runs a backfill, and asserts that applications appear
 * with events, provenance and confidence attached.
 *
 * Every component in the path is the production one: only Google is stubbed.
 */

const DATABASE_URL = process.env.TEST_DATABASE_URL ?? 'postgres://loop:loop@localhost:55432/loop';
const STUB_PORT = process.env.STUB_PORT ?? '8787';

const env = {
  ...process.env,
  DATABASE_URL,
  LOOP_KEK: process.env.LOOP_KEK ?? Buffer.alloc(32, 3).toString('base64'),
  GOOGLE_CLIENT_ID: 'stub',
  GOOGLE_CLIENT_SECRET: 'stub',
  GOOGLE_API_BASE: `http://localhost:${STUB_PORT}`,
  GOOGLE_OAUTH_BASE: `http://localhost:${STUB_PORT}`,
  MODEL_BASE_URL: '',
  LOG_LEVEL: process.env.LOG_LEVEL ?? 'warn',
  QUIET_HOURS: '21:00-08:00',
};

const children = [];
const start = (name, file, extra = {}) => {
  const child = spawn('node', ['--import', 'tsx', file], {
    env: { ...env, ...extra },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdout.on('data', (d) => process.env.E2E_VERBOSE && process.stdout.write(`[${name}] ${d}`));
  child.stderr.on('data', (d) => process.stderr.write(`[${name}] ${d}`));
  children.push(child);
  return child;
};

const stop = () => {
  for (const c of children) c.kill('SIGTERM');
};
process.on('exit', stop);
process.on('SIGINT', () => { stop(); process.exit(1); });

const pool = new pg.Pool({ connectionString: DATABASE_URL });

function fail(message, detail) {
  console.error(`\n  ✗ ${message}`);
  if (detail) console.error(`    ${detail}`);
  stop();
  process.exit(1);
}

async function main() {
  console.log('\n  end to end · stub mailbox → applications\n');

  // ── a clean tenant ───────────────────────────────────────────────────────
  const email = `e2e-${Date.now()}@example.com`;
  const user = await pool.query('insert into users (email, tz) values ($1, $2) returning id', [
    email,
    'Europe/Rome',
  ]);
  const userId = user.rows[0].id;
  await pool.query('select seed_stage_defs($1)', [userId]);
  console.log(`  · tenant ${userId.slice(0, 8)}`);

  // ── a mailbox pointing at the stub ───────────────────────────────────────
  const { initCrypto, generateDek, seal, wrapDek } = await import('../packages/db/dist/crypto.js');
  await initCrypto();
  const kek = Buffer.from(env.LOOP_KEK, 'base64');
  const dek = generateDek();
  const wrapped = wrapDek(dek, kek);
  const sealed = seal(JSON.stringify({ refresh_token: 'stub-refresh-token' }), dek);

  const mailbox = await pool.query(
    `insert into mailbox_accounts
       (user_id, provider, address, secret_ciphertext, secret_nonce, dek_wrapped, dek_nonce, scopes, status)
     values ($1,'gmail',$2,$3,$4,$5,$6,'{gmail.readonly}','ok') returning id`,
    [userId, email, sealed.ciphertext, sealed.nonce, wrapped.ciphertext, wrapped.nonce],
  );
  const mailboxId = mailbox.rows[0].id;
  console.log(`  · mailbox ${mailboxId.slice(0, 8)} → stub`);

  // ── the stub, then the services ──────────────────────────────────────────
  start('stub', 'scripts/stub-google.mjs');
  await sleep(700);

  start('classifier', 'services/classifier/src/index.ts', { DB_ROLE: 'loop_classifier', HEALTH_PORT: '9201' });
  start('extractor', 'services/extractor/src/index.ts', { DB_ROLE: 'loop_extractor', HEALTH_PORT: '9202' });
  start('resolver', 'services/resolver/src/index.ts', { DB_ROLE: 'loop_resolver', HEALTH_PORT: '9203' });
  start('pipeline', 'services/pipeline/src/index.ts', { DB_ROLE: 'loop_pipeline', HEALTH_PORT: '9204' });
  start('connector', 'services/connector/src/index.ts', { DB_ROLE: 'loop_connector', HEALTH_PORT: '9205' });
  console.log('  · five services up');
  await sleep(3500);

  // ── the first scan ───────────────────────────────────────────────────────
  await pool.query('select pg_notify($1, $2)', [
    'loop_backfill',
    JSON.stringify({ mailbox_id: mailboxId, months: 12 }),
  ]);
  console.log('  · backfill requested');

  // ── wait for the pipeline to settle ──────────────────────────────────────
  let applications = 0;
  let events = 0;
  for (let i = 0; i < 60; i++) {
    await sleep(1000);
    const a = await pool.query('select count(*)::int as n from applications where user_id = $1', [userId]);
    const e = await pool.query('select count(*)::int as n from application_events where user_id = $1', [userId]);
    const settled = a.rows[0].n === applications && e.rows[0].n === events && applications > 0;
    applications = a.rows[0].n;
    events = e.rows[0].n;
    process.stdout.write(`\r  · ${applications} applications, ${events} events`);
    if (settled && i > 6) break;
  }
  console.log('');

  // ── the assertions ───────────────────────────────────────────────────────
  const seen = await pool.query(
    `select outcome, count(*)::int as n from seen_messages where user_id = $1 group by outcome order by outcome`,
    [userId],
  );
  const outcomes = Object.fromEntries(seen.rows.map((r) => [r.outcome ?? 'pending', r.n]));
  console.log(`  · messages ${JSON.stringify(outcomes)}`);

  if (applications === 0) fail('no application was created from the stub mailbox');
  if (events === 0) fail('no event reached the log');
  if (!outcomes.dropped) fail('the classifier dropped nothing — the negatives should not have survived');
  if (!outcomes.placed) fail('nothing was placed');

  // Provenance and confidence on every automated event.
  const bad = await pool.query(
    `select count(*)::int as n from application_events
      where user_id = $1 and rung is not null and rung < 4
        and (evidence_ref is null or confidence is null)`,
    [userId],
  );
  if (bad.rows[0].n > 0) fail(`${bad.rows[0].n} automated events lack evidence or confidence`);

  // Exactly one first touch per application.
  const touches = await pool.query(
    `select application_id, count(*)::int as n from sources
      where user_id = $1 and is_first_touch group by application_id having count(*) > 1`,
    [userId],
  );
  if (touches.rowCount > 0) fail('an application has more than one first touch');

  // The review queue holds what the model would have taken.
  const review = await pool.query(
    `select count(*)::int as n from review_items where user_id = $1 and resolved_at is null`,
    [userId],
  );

  const sample = await pool.query(
    `select c.canonical_name as company, a.role_title, a.current_stage, a.current_phase, a.confidence,
            (select count(*)::int from application_events e where e.application_id = a.id) as events
       from applications a join companies c on c.id = a.company_id
      where a.user_id = $1 order by c.canonical_name limit 8`,
    [userId],
  );

  console.log('\n  applications');
  for (const r of sample.rows) {
    console.log(
      `  · ${String(r.company).padEnd(22)} ${String(r.current_stage).padEnd(14)} ${String(r.current_phase).padEnd(13)} conf ${r.confidence}  ${r.events} events`,
    );
  }
  console.log(`\n  review queue: ${review.rows[0].n} item(s) — the messages only rung 3 could place`);
  console.log('\n  ✓ a message went from the mailbox to the pipeline without anyone typing anything\n');

  await pool.query('select erase_user($1)', [userId]);
  await pool.end();
  stop();
  process.exit(0);
}

main().catch((err) => {
  console.error(err);
  stop();
  process.exit(1);
});
