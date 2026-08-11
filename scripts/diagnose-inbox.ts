/**
 * Ground truth for the go/no-go.
 *
 * The corpus in `fixtures/` measures the rules against messages we wrote
 * ourselves, which proves only that the rules still do what they did yesterday.
 * This measures them against the mailbox they exist to read: it asks Gmail
 * directly for mail that is certainly about job applications, then looks up
 * what Loop decided about each of those messages.
 *
 * That cross-reference is the whole point. It separates the two failures that
 * look identical from the dashboard:
 *
 *   · the classifier dropped it        → a false negative, invisible and
 *                                        unrecoverable, the failure §07 says
 *                                        to bias against above all else
 *   · the classifier passed it and the
 *     extractor could not place it     → a review item, visible and fixable
 *
 * Read-only. It never writes to the database and never stores a subject.
 * Subjects are printed to the operator's own terminal, and only with --samples,
 * because the person running this is the person whose mailbox it is.
 */
import pg from 'pg';
import { GoogleClient, readRefreshToken, type MailboxRow } from '@loop/google';

const showSamples = process.argv.includes('--samples');
const sampleSize = 6;

/**
 * The probes must be bounded to the same window the backfill read, or every
 * message older than the scan counts as a miss and the recall figure is a
 * measurement of the date range rather than of the classifier.
 */
const monthsArg = process.argv.find((a) => a.startsWith('--months='));
const months = monthsArg ? Number(monthsArg.split('=')[1]) : 12;
const since = new Date();
since.setMonth(since.getMonth() - months);
const AFTER = `after:${since.toISOString().slice(0, 10).replace(/-/g, '/')}`;

const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL });

const mailboxes = await pool.query<MailboxRow>(
  `select * from mailbox_accounts where provider = 'gmail' limit 1`,
);
const mailbox = mailboxes.rows[0];
if (!mailbox) {
  console.error('no gmail mailbox connected');
  process.exit(1);
}

const google = new GoogleClient({
  clientId: process.env.GOOGLE_CLIENT_ID!,
  clientSecret: process.env.GOOGLE_CLIENT_SECRET!,
});
const { access_token: token } = await google.refresh(await readRefreshToken(mailbox));

/**
 * Each probe is a Gmail query whose hits are, by construction, about a job
 * application. They overlap on purpose — a message caught by two probes is
 * counted in both, because what matters is coverage per category, not a
 * partition of the mailbox.
 */
const ATS_DOMAINS = [
  'greenhouse-mail.io', 'greenhouse.io', 'hire.lever.co', 'lever.co', 'myworkday.com',
  'myworkdayjobs.com', 'workday.com', 'ashbyhq.com', 'smartrecruiters.com',
  'workable.com', 'workablemail.com', 'icims.com', 'taleo.net', 'taleo.com',
  'recruitee.com', 'bamboohr.com', 'successfactors.com', 'oraclecloud.com',
  'teamtailor.com', 'personio.de', 'jobvite.com', 'breezy.hr',
];

const PROBES: Array<{ name: string; q: string }> = [
  { name: 'mittenti ATS noti', q: ATS_DOMAINS.map((d) => `from:${d}`).join(' OR ') },
  { name: 'LinkedIn — candidature', q: 'from:linkedin.com (subject:application OR subject:candidatura OR subject:"your application")' },
  { name: 'Indeed — candidature', q: 'from:(indeed.com OR indeedemail.com OR match.indeed.com)' },
  { name: 'conferme di invio (it)', q: 'subject:(candidatura OR "abbiamo ricevuto" OR "la tua candidatura" OR colloquio OR selezione)' },
  { name: 'conferme di invio (en)', q: 'subject:("your application" OR "application received" OR "thank you for applying" OR "application to")' },
  { name: 'esiti negativi (it+en)', q: 'subject:("non siamo" OR "future opportunità" OR "not moving forward" OR "unfortunately" OR "other candidates")' },
  { name: 'colloqui / inviti', q: 'subject:(interview OR colloquio OR "availability" OR "disponibilità")' },
  { name: 'offerte', q: 'subject:("offer" OR "offerta" OR "job offer" OR "proposta di assunzione")' },
];

async function idsFor(q: string, cap = 500): Promise<string[]> {
  const out: string[] = [];
  let pageToken: string | undefined;
  do {
    const page = await google.listMessages(token, q, pageToken, 100);
    for (const m of page.messages ?? []) out.push(m.id);
    pageToken = page.nextPageToken;
  } while (pageToken && out.length < cap);
  return out;
}

interface Verdict {
  total: number;
  dropped: number;
  review: number;
  placed: number;
  unseen: number;
}

async function verdictFor(ids: string[]): Promise<Verdict> {
  if (ids.length === 0) return { total: 0, dropped: 0, review: 0, placed: 0, unseen: 0 };
  const res = await pool.query<{ outcome: string | null; n: string }>(
    `select outcome, count(*)::text as n
       from seen_messages
      where mailbox_id = $1 and provider_message_id = any($2)
      group by outcome`,
    [mailbox.id, ids],
  );
  const by = new Map(res.rows.map((r) => [r.outcome ?? 'in_flight', Number(r.n)]));
  const seen = [...by.values()].reduce((a, b) => a + b, 0);
  return {
    total: ids.length,
    dropped: by.get('dropped') ?? 0,
    review: by.get('review') ?? 0,
    placed: by.get('placed') ?? 0,
    unseen: ids.length - seen,
  };
}

console.log(`\nmailbox ${mailbox.address} · finestra: ultimi ${months} mesi (${AFTER})\n`);
console.log(
  'sonda'.padEnd(28) + 'trovati'.padStart(9) + 'scartati'.padStart(10) +
  'review'.padStart(9) + 'piazzati'.padStart(10) + 'mai letti'.padStart(11),
);
console.log('─'.repeat(77));

const allJobIds = new Set<string>();

for (const probe of PROBES) {
  const ids = await idsFor(`${AFTER} (${probe.q})`);
  ids.forEach((id) => allJobIds.add(id));
  const v = await verdictFor(ids);
  console.log(
    probe.name.padEnd(28) +
      String(v.total).padStart(9) +
      String(v.dropped).padStart(10) +
      String(v.review).padStart(9) +
      String(v.placed).padStart(10) +
      String(v.unseen).padStart(11),
  );

  if (showSamples && ids.length) {
    for (const id of ids.slice(0, sampleSize)) {
      const msg = await google.getMessage(token, id);
      const h = (n: string): string =>
        msg.payload?.headers?.find((x) => x.name.toLowerCase() === n)?.value ?? '';
      const row = await pool.query<{ outcome: string | null }>(
        `select outcome from seen_messages where mailbox_id = $1 and provider_message_id = $2`,
        [mailbox.id, id],
      );
      const outcome = row.rows[0] ? (row.rows[0].outcome ?? 'in volo') : 'MAI LETTO';
      console.log(`    [${outcome.padEnd(9)}] ${h('from').slice(0, 42).padEnd(42)} ${h('subject').slice(0, 70)}`);
    }
  }
}

console.log('─'.repeat(77));
const union = await verdictFor([...allJobIds]);
console.log(
  'UNIONE (dedup)'.padEnd(28) +
    String(union.total).padStart(9) +
    String(union.dropped).padStart(10) +
    String(union.review).padStart(9) +
    String(union.placed).padStart(10) +
    String(union.unseen).padStart(11),
);

const reachedLadder = union.review + union.placed;
console.log(`
  recall del classifier   ${union.total ? ((reachedLadder / union.total) * 100).toFixed(1) : '—'}%   (quanta posta di lavoro è arrivata alla scala di estrazione)
  resa dell'estrattore    ${reachedLadder ? ((union.placed / reachedLadder) * 100).toFixed(1) : '—'}%   (di quella, quanta è diventata una candidatura)
`);

await pool.end();
