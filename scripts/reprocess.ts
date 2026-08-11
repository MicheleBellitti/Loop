/**
 * Re-run the whole ladder over mail that was already read.
 *
 * `seen_messages` is the replay log — "everything downstream of the connector is
 * replayable from seen_messages" — and this is the operation that log exists
 * for. When a rule improves, last month's mail should be re-derived rather than
 * re-fetched from scratch and re-dated; the spec calls this out as one of the
 * reasons the system is event-sourced at all.
 *
 * It clears the derived state for the user (applications, the event log, review
 * items, suggestions) and republishes every known message id to `raw_message`.
 * The message bodies are re-fetched from Gmail because we deliberately never
 * stored them.
 *
 *   npx tsx scripts/reprocess.ts            # dry run: says what it would do
 *   npx tsx scripts/reprocess.ts --commit
 */
import pg from 'pg';
import { GoogleClient, readRefreshToken, type MailboxRow } from '@loop/google';
import { toRawMessage } from '../services/connector/src/normalise.js';
import { publish, QUEUES } from '@loop/queue';

const commit = process.argv.includes('--commit');
const limit = Number(process.argv.find((a) => a.startsWith('--limit='))?.split('=')[1] ?? 100_000);

const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL });
const mailbox = (
  await pool.query<MailboxRow>(`select * from mailbox_accounts where provider = 'gmail' limit 1`)
).rows[0];
if (!mailbox) {
  console.error('no gmail mailbox connected');
  process.exit(1);
}
const userId = mailbox.user_id;

const counts = await pool.query<{ n: string; what: string }>(
  `select count(*)::text as n, 'messaggi da rigiocare' as what from seen_messages where mailbox_id = $1
   union all select count(*)::text, 'applications da cancellare' from applications where user_id = $2
   union all select count(*)::text, 'eventi da cancellare' from application_events where user_id = $2
   union all select count(*)::text, 'review da cancellare' from review_items where user_id = $2`,
  [mailbox.id, userId],
);
for (const r of counts.rows) console.log(`  ${String(r.n).padStart(6)}  ${r.what}`);

if (!commit) {
  console.log('\ndry run — nessuna modifica. Aggiungi --commit per procedere.');
  await pool.end();
  process.exit(0);
}

// ── clear the derived state ────────────────────────────────────────────────
// Everything removed here is a projection of the mail, and the mail is still in
// the mailbox. `loop.erasing` is the append-only trigger's one escape hatch.
const client = await pool.connect();
await client.query('begin');
await client.query(`set local loop.erasing = 'on'`);
for (const table of [
  'application_events',
  'applications',
  'review_items',
  'suggestions',
  'notifications_sent',
]) {
  await client.query(`delete from ${table} where user_id = $1`, [userId]);
}
await client.query(`update seen_messages set outcome = null, processed_at = null, park_attempts = 0
                     where mailbox_id = $1`, [mailbox.id]);
// Learned company aliases are derived too, and leaving them behind makes a
// replay unable to correct a bad name: the alias still points at the row the
// old code created, so the fixed extractor looks it up and finds the mistake.
await client.query(`delete from company_aliases where user_id = $1`, [userId]);
await client.query(
  `delete from companies c
    where not exists (select 1 from applications a where a.company_id = c.id)
      and not exists (select 1 from company_aliases al where al.company_id = c.id)`,
);
await client.query(`delete from mq.messages`);
await client.query('commit');
client.release();
console.log('\nstato derivato azzerato\n');

// ── republish ──────────────────────────────────────────────────────────────
const google = new GoogleClient({
  clientId: process.env.GOOGLE_CLIENT_ID!,
  clientSecret: process.env.GOOGLE_CLIENT_SECRET!,
});
let token = (await google.refresh(await readRefreshToken(mailbox))).access_token;
let refreshedAt = Date.now();

/**
 * `--ids-from=survey.json` narrows the replay to the messages that actually
 * reached the extraction ladder last time.
 *
 * Re-fetching an entire year takes hours, and it is hours spent on mail the
 * classifier already refused — and only ever got stricter about, never laxer,
 * so a message it dropped before it would drop again. The messages that can
 * produce a different answer are the ones that got past it.
 */
const idsFrom = process.argv.find((a) => a.startsWith('--ids-from='))?.split('=')[1];
let messageIds: string[];
if (idsFrom) {
  const { readFile } = await import('node:fs/promises');
  const survey = JSON.parse(await readFile(idsFrom, 'utf8')) as Array<{ id: string }>;
  // In the order they arrived, always.
  //
  // The survey is written newest-first, and replaying it that way hands the
  // resolver a rejection before the acknowledgement that would have created the
  // application to reject — so it creates a second one, and a single process
  // ends up as two rows with two halves of its history. Live ingestion is
  // chronological by construction; a replay that is not does not reproduce it.
  const order = new Map(survey.map((s, i) => [s.id, i]));
  const rows = await pool.query<{ provider_message_id: string }>(
    `select provider_message_id from seen_messages
      where mailbox_id = $1 and provider_message_id = any($2)
      order by received_at asc`,
    [mailbox.id, [...order.keys()]],
  );
  messageIds = rows.rows.map((r) => r.provider_message_id).slice(0, limit);
} else {
  const res = await pool.query<{ provider_message_id: string }>(
    `select provider_message_id from seen_messages where mailbox_id = $1 order by received_at limit $2`,
    [mailbox.id, limit],
  );
  messageIds = res.rows.map((r) => r.provider_message_id);
}
const ids = { rows: messageIds.map((id) => ({ provider_message_id: id })) };

let published = 0;
let failed = 0;
let done = 0;
// Four at a time: enough to hide the round trip, far under any Gmail quota.
const CONCURRENCY = 4;
for (let i = 0; i < ids.rows.length; i += CONCURRENCY) {
  // An access token lasts an hour; a full reprocess can outlive one.
  if (Date.now() - refreshedAt > 40 * 60_000) {
    token = (await google.refresh(await readRefreshToken(mailbox))).access_token;
    refreshedAt = Date.now();
  }
  const slice = ids.rows.slice(i, i + CONCURRENCY);
  await Promise.all(
    slice.map(async (row) => {
      try {
        const gmail = await google.hydrateCalendarParts(
          token,
          await google.getMessage(token, row.provider_message_id),
        );
        const raw = toRawMessage(gmail, { userId, mailboxId: mailbox.id, backfill: true });
        await publish(pool, QUEUES.raw, raw);
        published += 1;
      } catch {
        failed += 1;
      }
      done += 1;
    }),
  );
  if (done % 100 < CONCURRENCY) console.log(`  ${done}/${ids.rows.length}…`);
}

console.log(`\nripubblicati ${published} messaggi (${failed} falliti). I consumer li lavorano ora.`);
await pool.end();
