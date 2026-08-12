/**
 * The reference verdict, for the Python port to be diffed against.
 *
 * Reads every message this mailbox has already seen, runs the classifier and
 * rungs 1 and 2 exactly as the extractor does, and writes one line per message
 * containing both the input and what this implementation made of it.
 *
 * That pairing is the point: the file is a self-contained corpus, so
 * `diff_against_ts.py` needs no database, no network and no mailbox, and the
 * comparison stays reproducible after the mail has moved on.
 *
 * It contains real message bodies and goes to `fixtures/private/`, which is
 * git-ignored. Nothing here writes to a log.
 */
import { mkdir, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import pg from 'pg';
import { applyRules, atsDomains, loadRules, vendorForDomain } from '@loop/rules';
import { domainOfAddress, normaliseMessage, type CalendarInvite, type RawMessage } from '@loop/domain';
import { GoogleClient, readRefreshToken, type MailboxRow } from '@loop/google';
import { classify } from '../services/classifier/src/classify.js';
import { runRung2 } from '../services/extractor/src/rung2.js';
import { parseIcs } from '../services/connector/src/normalise.js';

const arg = (name: string, fallback: string): string =>
  process.argv.find((a) => a.startsWith(`--${name}=`))?.split('=')[1] ?? fallback;

const out = arg('out', 'fixtures/private/ladder-baseline.jsonl');
const limit = Number(arg('limit', '1000'));

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

const registry = await loadRules();
const domains = atsDomains(registry);

// The same context the classifier and rung 2 run with in production. Diffing
// without it compares two different questions: a reply on an owned thread is
// worth two points and inherits an application.
// Scoped to the user, because the rows are. Reading this pool without setting
// `loop.user_id` first is how a previous measurement came to count another
// tenant's integration-test data as this mailbox's.
const client = await pool.connect();
await client.query('select set_config($1,$2,true)', ['loop.user_id', mailbox.user_id]);

const threadRows = await client.query<{ thread_id: string; application_id: string }>(
  `select distinct payload->>'thread_id' as thread_id, application_id
     from application_events
    where user_id = $1 and payload ? 'thread_id'`,
  [mailbox.user_id],
);
const threadToApplication = new Map(threadRows.rows.map((r) => [r.thread_id, r.application_id]));

// Verbatim from services/classifier/src/index.ts: the same two sets, derived
// the same way. A baseline built from a different context is not a baseline.
const companyRows = await client.query<{ domain: string }>(
  `select distinct c.domain from companies c
     join applications a on a.company_id = c.id
    where a.user_id = $1 and c.domain is not null`,
  [mailbox.user_id],
);
const newsletterRows = await client.query<{ domain: string }>(
  `select split_part(provider_message_id, '@', 2) as domain
     from seen_messages
    where user_id = $1 and outcome = 'dropped'
    group by 1 having count(*) >= 5`,
  [mailbox.user_id],
);

const companyDomains = new Set(companyRows.rows.map((r) => r.domain.toLowerCase()));
const knownNewsletters = new Set(newsletterRows.rows.map((r) => r.domain).filter(Boolean));

const rows = await client.query<{ provider_message_id: string; outcome: string }>(
  `select provider_message_id, outcome
     from seen_messages
    where mailbox_id = $1
    order by received_at desc
    limit $2`,
  [mailbox.id, limit],
);
client.release();

const lines: string[] = [
  JSON.stringify({
    kind: 'context',
    company_domains: [...companyDomains],
    known_threads: [...threadToApplication.keys()],
    known_newsletters: [...knownNewsletters],
    thread_to_application: Object.fromEntries(threadToApplication),
  }),
];

let read = 0;
for (const row of rows.rows) {
  const msg = await toRawMessage(row.provider_message_id).catch(() => null);
  if (!msg) continue;
  read += 1;

  const classification = classify(msg, {
    atsDomains: domains,
    companyDomains,
    knownThreads: new Set(threadToApplication.keys()),
    knownNewsletters,
  });

  const verdict: Record<string, unknown> = {
    score: classification.score,
    outcome: classification.outcome,
    vendor: vendorForDomain(registry, domainOfAddress(msg.headers.from)),
  };

  if (classification.outcome !== 'drop') {
    const match = applyRules(registry, msg);
    const rung2 = runRung2(msg, { threadToApplication, atsDomains: domains });
    if (match) {
      Object.assign(verdict, {
        intent: match.intent,
        confidence: match.confidence,
        company: match.company,
        role: match.role,
        rung: 1,
        stage_hint: stageForIntent(match.intent),
      });
    } else if (rung2 && rung2.intent !== 'other') {
      Object.assign(verdict, {
        intent: rung2.intent,
        confidence: rung2.confidence,
        company: rung2.company,
        role: null,
        rung: 2,
        stage_hint: rung2.stageHint ?? stageForIntent(rung2.intent),
      });
    }
  }

  lines.push(JSON.stringify({ message: msg, verdict }));
}

await mkdir(dirname(join(process.cwd(), out)), { recursive: true });
await writeFile(out, `${lines.join('\n')}\n`, 'utf8');
console.log(`${read} messaggi → ${out}`);
await pool.end();

/** The extractor's own mapping, duplicated here so the file records what it did. */
function stageForIntent(intent: string): string | null {
  const table: Record<string, string> = {
    applied: 'applied',
    acknowledged: 'acknowledged',
    schedule_screening: 'recruiter_reachout',
    interview_invite: 'technical',
    take_home: 'take_home',
    offer: 'offer',
    negotiation: 'negotiating',
  };
  return table[intent] ?? null;
}

async function toRawMessage(id: string): Promise<RawMessage> {
  const gmail = await google.getMessage(token, id);
  const header = (name: string): string =>
    gmail.payload?.headers?.find((h) => h.name.toLowerCase() === name)?.value ?? '';

  const parts = flatten(gmail.payload);
  const text = await bodyOf(parts.find((p) => p.mimeType === 'text/plain'), id);
  const html = await bodyOf(parts.find((p) => p.mimeType === 'text/html'), id);
  const ics = await bodyOf(parts.find((p) => p.mimeType?.startsWith('text/calendar')), id);

  const normalised = normaliseMessage(text ? { text } : { html: html ?? '' });
  let invite: CalendarInvite | null = null;
  if (ics) invite = parseIcs(ics);

  return {
    user_id: mailbox!.user_id,
    mailbox_id: mailbox!.id,
    provider_message_id: id,
    thread_id: gmail.threadId ?? null,
    received_at: new Date(Number(gmail.internalDate ?? Date.now())).toISOString(),
    headers: {
      message_id: header('message-id') || id,
      from: header('from'),
      to: header('to').split(',').map((s) => s.trim()).filter(Boolean),
      subject: header('subject'),
      date: header('date'),
      list_id: header('list-id') || null,
      list_unsubscribe: header('list-unsubscribe') || null,
      precedence: header('precedence') || null,
    },
    text: normalised.text,
    body_sha256: '',
    invite,
  };
}

interface Part {
  mimeType?: string;
  body?: { data?: string; attachmentId?: string };
  parts?: Part[];
}

function flatten(part: Part | undefined): Part[] {
  if (!part) return [];
  return [part, ...(part.parts ?? []).flatMap(flatten)];
}

/** A part is either inline or an attachment that has to be fetched by id. */
async function bodyOf(part: Part | undefined, messageId: string): Promise<string | null> {
  if (!part?.body) return null;
  if (part.body.data) return Buffer.from(part.body.data, 'base64url').toString('utf8');
  if (!part.body.attachmentId) return null;
  const attachment = await google.getAttachment(token, messageId, part.body.attachmentId);
  return attachment?.data ? Buffer.from(attachment.data, 'base64url').toString('utf8') : null;
}
