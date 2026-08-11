/**
 * Replay the ladder over real mail, without touching the database.
 *
 * Iterating on the rules by re-running the whole pipeline is far too slow a
 * loop, and it mutates state you then have to clean up. This fetches messages
 * that were already read, runs them through the real classifier and the real
 * rung 1 and rung 2, and prints what each rung decided — so a rule change can
 * be judged in seconds against the mailbox it exists to read.
 *
 * Read-only against both Gmail and Postgres.
 */
import pg from 'pg';
import { GoogleClient, readRefreshToken, type MailboxRow } from '@loop/google';
import { toRawMessage } from '../services/connector/src/normalise.js';
import { classify } from '../services/classifier/src/classify.js';
import { runRung2 } from '../services/extractor/src/rung2.js';
import { applyRules, atsDomains, rules } from '@loop/rules';

const limit = Number(process.argv.find((a) => a.startsWith('--limit='))?.split('=')[1] ?? 120);
const only = process.argv.find((a) => a.startsWith('--domain='))?.split('=')[1];
const showDropped = process.argv.includes('--dropped');
const verbose = process.argv.includes('--why');

const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL });
const mailbox = (
  await pool.query<MailboxRow>(`select * from mailbox_accounts where provider = 'gmail' limit 1`)
).rows[0];
if (!mailbox) {
  console.error('no gmail mailbox connected');
  process.exit(1);
}

const google = new GoogleClient({
  clientId: process.env.GOOGLE_CLIENT_ID!,
  clientSecret: process.env.GOOGLE_CLIENT_SECRET!,
});
const { access_token: token } = await google.refresh(await readRefreshToken(mailbox));

const all = await rules();
const domains = atsDomains(all);

/**
 * `--ids-from=file.json` replays exactly the messages a survey turned up, which
 * is how a single vendor's mail gets iterated on without paging the mailbox.
 */
const idsFrom = process.argv.find((a) => a.startsWith('--ids-from='))?.split('=')[1];
let ids: { rows: Array<{ provider_message_id: string; outcome: string }> };

if (idsFrom) {
  const { readFile } = await import('node:fs/promises');
  const survey = JSON.parse(await readFile(idsFrom, 'utf8')) as Array<{
    id: string;
    outcome: string;
    domain: string;
    from: string;
  }>;
  ids = {
    rows: survey
      .filter((s) => !only || s.domain.includes(only) || s.from.includes(only))
      .slice(0, limit)
      .map((s) => ({ provider_message_id: s.id, outcome: s.outcome })),
  };
} else {
  const wanted = showDropped
    ? `outcome in ('review','placed','dropped')`
    : `outcome in ('review','placed')`;
  ids = await pool.query<{ provider_message_id: string; outcome: string }>(
    `select provider_message_id, outcome from seen_messages
      where mailbox_id = $1 and ${wanted}
      order by received_at desc limit $2`,
    [mailbox.id, limit],
  );
}

const tally = { pass: 0, cheap: 0, drop: 0, rung1: 0, rung2: 0, none: 0 };
const missed: string[] = [];

for (const row of ids.rows) {
  let raw;
  try {
    const msg = await google.hydrateCalendarParts(token, await google.getMessage(token, row.provider_message_id));
    raw = toRawMessage(msg, mailbox.id);
  } catch {
    continue;
  }
  if (only && !raw.headers.from.includes(only)) continue;

  const c = classify(raw, {
    atsDomains: domains,
    companyDomains: new Set<string>(),
    knownThreads: new Set<string>(),
    knownNewsletters: new Set<string>(),
  });
  tally[c.outcome === 'pass' ? 'pass' : c.outcome === 'cheap_only' ? 'cheap' : 'drop']++;

  const r1 = applyRules(all, raw);
  const r2 = r1 ? null : runRung2(raw, { atsDomains: domains, threadToApplication: new Map<string, string>() });
  if (r1) tally.rung1++;
  else if (r2) tally.rung2++;
  else tally.none++;

  const verdict = r1
    ? `rung1 ${r1.vendor}/${r1.intent} company=${JSON.stringify(r1.company ?? null)} role=${JSON.stringify(r1.role ?? null)}`
    : r2
      ? `rung2 ${r2.intent} stage=${r2.stage ?? '—'}`
      : c.outcome === 'drop'
        ? 'dropped'
        : 'ABSTAIN → review';

  if (!r1 && !r2 && c.outcome !== 'drop') missed.push(raw.headers.subject);

  const mark = r1 ? '✓' : r2 ? '·' : c.outcome === 'drop' ? ' ' : '✗';
  console.log(
    `${mark} [${String(c.score).padStart(3)}] ${raw.headers.subject.slice(0, 62).padEnd(62)} ${verdict}`,
  );
  if (verbose) console.log(`      ${c.reasons.join(' | ')}`);
}

console.log(`
  classifier   pass ${tally.pass}  ·  cheap_only ${tally.cheap}  ·  drop ${tally.drop}
  estrazione   rung1 ${tally.rung1}  ·  rung2 ${tally.rung2}  ·  abstain ${tally.none}
`);

await pool.end();
