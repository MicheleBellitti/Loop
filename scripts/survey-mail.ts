/**
 * What the mail actually looks like.
 *
 * The rules in `rules/ats/` were written from the spec's example rule and
 * tested against fixtures written from the same reading — so CI reported
 * perfect precision and recall while the patterns matched almost nothing in a
 * real mailbox. §17 asked for the opposite order: build the corpus from real
 * mail on day one, then write the rules to it. This is that first step.
 *
 * It prints sender domain and subject for every message the classifier passed,
 * grouped by domain, so the patterns can be derived from evidence.
 *
 * Read-only, and local: subjects go to the operator's terminal, never to a log
 * and never to disk unless --out is given.
 */
import { writeFile } from 'node:fs/promises';
import pg from 'pg';
import { GoogleClient, readRefreshToken, type MailboxRow } from '@loop/google';

const outArg = process.argv.find((a) => a.startsWith('--out='));
const limit = Number(process.argv.find((a) => a.startsWith('--limit='))?.split('=')[1] ?? 400);

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

// Everything the classifier let through: the review queue plus whatever was
// actually placed. These are the messages the rules had a chance at and missed.
const rows = await pool.query<{ provider_message_id: string; outcome: string }>(
  `select provider_message_id, outcome
     from seen_messages
    where mailbox_id = $1 and outcome in ('review', 'placed')
    order by received_at desc
    limit $2`,
  [mailbox.id, limit],
);

interface Seen {
  from: string;
  domain: string;
  subject: string;
  outcome: string;
  id: string;
  snippet: string;
}

const seen: Seen[] = [];
for (const row of rows.rows) {
  try {
    const msg = await google.getMessage(token, row.provider_message_id);
    const header = (n: string): string =>
      msg.payload?.headers?.find((h) => h.name.toLowerCase() === n)?.value ?? '';
    const from = header('from');
    const domain = /@([A-Za-z0-9.-]+)>?/.exec(from)?.[1]?.toLowerCase().replace(/[>.]$/, '') ?? '(none)';
    seen.push({
      from,
      domain,
      subject: header('subject'),
      outcome: row.outcome,
      id: row.provider_message_id,
      snippet: (msg.snippet ?? '').slice(0, 160),
    });
  } catch {
    // A message can disappear between the scan and now; it is not the point.
  }
}

const byDomain = new Map<string, Seen[]>();
for (const s of seen) {
  const list = byDomain.get(s.domain) ?? [];
  list.push(s);
  byDomain.set(s.domain, list);
}

const ordered = [...byDomain.entries()].sort((a, b) => b[1].length - a[1].length);

console.log(`\n${seen.length} messaggi passati al ladder, ${ordered.length} domini mittenti\n`);
for (const [domain, list] of ordered) {
  console.log(`\n── ${domain}  (${list.length})`);
  const subjects = new Set<string>();
  for (const s of list) {
    if (subjects.has(s.subject)) continue;
    subjects.add(s.subject);
    console.log(`   [${s.outcome}] ${s.subject}`);
  }
}

if (outArg) {
  const path = outArg.split('=')[1]!;
  await writeFile(path, JSON.stringify(seen, null, 2), 'utf8');
  console.log(`\nscritto ${path}`);
}

await pool.end();
