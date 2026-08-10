import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { applyRules, atsDomains, loadRules, type AtsRule } from '@loop/rules';
import { normaliseMessage, type CalendarInvite, type RawMessage } from '@loop/domain';
import { classify } from '../services/classifier/src/classify.js';
import { runRung2 } from '../services/extractor/src/rung2.js';

/**
 * The corpus runner.
 *
 * "CI runs the whole corpus on every commit and prints the confusion matrix."
 * It drives the real classifier and the real rung 1 and 2 — not a copy of their
 * logic — so a rule change that improves one vendor and breaks another shows up
 * as a number rather than as a surprise in production.
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..');

export interface Expectation {
  intent?: string;
  company?: string;
  vendor?: string;
  drop?: boolean;
  /** Only rung 3 can place this one. */
  requires_model?: boolean;
}

export interface CorpusCase {
  file: string;
  expect: Expectation;
}

/** A deliberately small .eml parser: fixtures are structured, not adversarial. */
export function parseEml(raw: string, name: string): RawMessage {
  const split = raw.indexOf('\n\n');
  const headerBlock = raw.slice(0, split);
  let body = raw.slice(split + 2);

  const headers: Record<string, string> = {};
  for (const line of headerBlock.replace(/\n[ \t]/g, ' ').split('\n')) {
    const idx = line.indexOf(':');
    if (idx > 0) headers[line.slice(0, idx).trim().toLowerCase()] = line.slice(idx + 1).trim();
  }

  let invite: CalendarInvite | null = null;
  const boundary = /boundary="([^"]+)"/.exec(headers['content-type'] ?? '')?.[1];
  if (boundary) {
    const parts = body.split(`--${boundary}`);
    const textPart = parts.find((p) => /content-type:\s*text\/plain/i.test(p));
    const icsPart = parts.find((p) => /content-type:\s*text\/calendar/i.test(p));
    body = textPart ? textPart.slice(textPart.indexOf('\n\n') + 2).trim() : '';
    if (icsPart) invite = parseInlineIcs(icsPart);
  }

  const normalised = normaliseMessage(
    /text\/html/i.test(headers['content-type'] ?? '') ? { html: body } : { text: body },
  );

  return {
    user_id: 'corpus',
    mailbox_id: 'corpus',
    provider_message_id: name,
    thread_id: null,
    received_at: new Date(headers.date ?? '2026-07-30T09:12:00Z').toISOString(),
    headers: {
      message_id: headers['message-id'] ?? name,
      from: headers.from ?? '',
      to: (headers.to ?? '').split(',').map((s) => s.trim()).filter(Boolean),
      subject: headers.subject ?? '',
      date: headers.date ?? '',
      list_id: headers['list-id'] ?? null,
      list_unsubscribe: headers['list-unsubscribe'] ?? null,
      precedence: headers.precedence ?? null,
    },
    text: normalised.text,
    body_sha256: createHash('sha256').update(normalised.text).digest('hex'),
    invite,
  };
}

function parseInlineIcs(part: string): CalendarInvite | null {
  const get = (k: string): string | null =>
    new RegExp(`^${k}(?:;[^:]*)?:(.*)$`, 'im').exec(part)?.[1]?.trim() ?? null;
  const uid = get('UID');
  const start = get('DTSTART');
  if (!uid || !start) return null;
  const iso = (v: string): string => {
    const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z?$/.exec(v);
    return m ? `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}Z` : v;
  };
  return {
    uid,
    summary: get('SUMMARY'),
    starts_at: iso(start),
    ends_at: get('DTEND') ? iso(get('DTEND')!) : null,
    location: get('LOCATION'),
    organiser: get('ORGANIZER')?.replace(/^mailto:/i, '') ?? null,
    attendees: [],
    status: (get('STATUS') ?? 'CONFIRMED').toLowerCase() === 'cancelled' ? 'cancelled' : 'confirmed',
    method: (get('METHOD') as CalendarInvite['method']) ?? null,
  };
}

export interface CaseResult {
  file: string;
  expected: Expectation;
  classified: 'pass' | 'cheap_only' | 'drop';
  intent: string | null;
  company: string | null;
  vendor: string | null;
  rung: number | null;
  ok: boolean;
  why: string;
}

export async function runCorpus(rules?: AtsRule[]): Promise<CaseResult[]> {
  const modelEnabled = !!process.env.MODEL_BASE_URL;
  const registry = rules ?? (await loadRules());
  const domains = atsDomains(registry);
  const manifest = JSON.parse(await readFile(join(ROOT, 'fixtures', 'manifest.json'), 'utf8')) as CorpusCase[];

  const results: CaseResult[] = [];
  for (const testCase of manifest) {
    const raw = await readFile(join(ROOT, testCase.file), 'utf8');
    const msg = parseEml(raw, testCase.file);

    const classification = classify(msg, {
      atsDomains: domains,
      companyDomains: new Set<string>(),
      knownThreads: new Set<string>(),
      knownNewsletters: new Set<string>(),
    });

    let intent: string | null = null;
    let company: string | null = null;
    let vendor: string | null = null;
    let rung: number | null = null;

    if (classification.outcome !== 'drop') {
      const match = applyRules(registry, msg);
      if (match) {
        intent = match.intent;
        company = match.company;
        vendor = match.vendor;
        rung = 1;
      } else {
        const two = runRung2(msg, { threadToApplication: new Map(), atsDomains: domains });
        if (two && two.intent !== 'other') {
          intent = two.intent;
          company = two.company;
          rung = 2;
        }
      }
    }

    let ok = true;
    let why = '';

    // A message that needs the model is judged on what rungs 1-2 must do with
    // it: keep it, abstain, and let it become a review item. Scoring it as a
    // miss would push the corpus towards rules that guess.
    if (testCase.expect.requires_model && !modelEnabled) {
      ok = classification.outcome !== 'drop' && intent === null;
      why = ok ? '' : `expected an abstain for the model, got ${intent ?? classification.outcome}`;
      results.push({
        file: testCase.file,
        expected: testCase.expect,
        classified: classification.outcome,
        intent,
        company,
        vendor,
        rung,
        ok,
        why,
      });
      continue;
    }

    if (testCase.expect.drop) {
      ok = classification.outcome === 'drop';
      why = ok ? '' : `expected a drop, classifier said ${classification.outcome}`;
    } else {
      if (classification.outcome === 'drop') {
        ok = false;
        why = 'classifier dropped a message it must keep';
      } else if (testCase.expect.intent) {
        if (intent !== testCase.expect.intent) {
          ok = false;
          why = `intent ${intent ?? 'none'} ≠ ${testCase.expect.intent}`;
        } else if (testCase.expect.company && normalise(company) !== normalise(testCase.expect.company)) {
          ok = false;
          why = `company ${company ?? 'none'} ≠ ${testCase.expect.company}`;
        } else if (testCase.expect.vendor && vendor !== testCase.expect.vendor) {
          ok = false;
          why = `vendor ${vendor ?? 'none'} ≠ ${testCase.expect.vendor}`;
        }
      }
    }

    results.push({
      file: testCase.file,
      expected: testCase.expect,
      classified: classification.outcome,
      intent,
      company,
      vendor,
      rung,
      ok,
      why,
    });
  }
  return results;
}

const normalise = (v: string | null): string => (v ?? '').toLowerCase().trim();

export interface CorpusReport {
  total: number;
  /** Cases the active configuration is actually expected to place. */
  scored: number;
  /** Cases that need rung 3, when rung 3 is off. */
  deferredToModel: number;
  passed: number;
  /** Of the messages we claimed an intent for, how many were right. */
  precision: number;
  /** Of the messages that have an intent, how many we found. */
  recall: number;
  /** A real application dropped by the classifier is unrecoverable. */
  falseNegatives: CaseResult[];
  failures: CaseResult[];
  byIntent: Map<string, { expected: number; found: number; correct: number }>;
}

export function summarise(results: CaseResult[]): CorpusReport {
  const modelEnabled = !!process.env.MODEL_BASE_URL;
  const scored = modelEnabled ? results : results.filter((r) => !r.expected.requires_model);
  const withIntent = scored.filter((r) => r.expected.intent);
  const claimed = scored.filter((r) => r.intent !== null);
  const correct = withIntent.filter((r) => r.intent === r.expected.intent);

  const byIntent = new Map<string, { expected: number; found: number; correct: number }>();
  for (const r of results) {
    const key = r.expected.intent ?? (r.expected.drop ? 'drop' : 'other');
    const entry = byIntent.get(key) ?? { expected: 0, found: 0, correct: 0 };
    entry.expected += 1;
    if (r.intent) entry.found += 1;
    if (r.ok) entry.correct += 1;
    byIntent.set(key, entry);
  }

  return {
    total: results.length,
    scored: scored.length,
    deferredToModel: results.length - scored.length,
    passed: results.filter((r) => r.ok).length,
    precision: claimed.length === 0 ? 1 : correct.length / claimed.length,
    recall: withIntent.length === 0 ? 1 : correct.length / withIntent.length,
    falseNegatives: results.filter((r) => !r.expected.drop && r.classified === 'drop'),
    failures: results.filter((r) => !r.ok),
    byIntent,
  };
}
