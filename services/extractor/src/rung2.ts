import { domainOfAddress, matchesDomainSuffix, type Intent, type RawMessage } from '@loop/domain';

/**
 * Rung 2 — calendar and thread heuristics.
 *
 * "An .ics attachment or a meeting link from a company domain resolves the
 * stage by itself. A reply on a known thread inherits the application with no
 * parsing at all." 13% of messages, 2 ms, €0.
 */

export interface Rung2Context {
  /** Threads already attached to an application, mapped to that application. */
  threadToApplication: ReadonlyMap<string, string>;
  /** Domains that belong to an ATS rather than to an employer. */
  atsDomains: readonly string[];
}

export interface Rung2Result {
  intent: Intent;
  confidence: number;
  stageHint: string | null;
  company: string | null;
  startsAt: string | null;
  endsAt: string | null;
  calendarEventId: string | null;
  applicationId: string | null;
  location: string | null;
}

/**
 * The stage a calendar title implies.
 *
 * Interview invitations name their own stage more often than any other signal
 * in the mailbox — "System & code review", "HR screening", "Final round" — and
 * a title match is both cheaper and more reliable than asking a model.
 */
const TITLE_TO_STAGE: Array<[RegExp, string]> = [
  [/\b(final|exec|leadership|founder|ceo|cto)\b/i, 'final'],
  [/\b(onsite|on-site|loop|super\s*day|assessment centre|full day)\b/i, 'onsite_loop'],
  [/\b(system\s*design|architecture|design (round|interview))\b/i, 'system_design'],
  [/\b(technical|coding|live\s*cod|pair\s*program|algorithm|code review)\b/i, 'technical'],
  [/\b(take[- ]home|assignment|exercise|challenge)\b/i, 'take_home'],
  [/\b(hr|people|talent|recruiter|screening|intro|introductory|first call|knowledge)\b/i, 'hr_call'],
];

export function stageFromTitle(title: string | null): string | null {
  if (!title) return null;
  for (const [re, stage] of TITLE_TO_STAGE) if (re.test(title)) return stage;
  return null;
}

export function runRung2(msg: RawMessage, ctx: Rung2Context): Rung2Result | null {
  const senderDomain = domainOfAddress(msg.headers.from);
  const isAts = !!senderDomain && ctx.atsDomains.some((d) => matchesDomainSuffix(senderDomain, d));

  // ── a calendar invite ────────────────────────────────────────────────────
  if (msg.invite) {
    const inv = msg.invite;
    const cancelled = inv.status === 'cancelled' || inv.method === 'CANCEL';
    const stage = stageFromTitle(inv.summary) ?? 'technical';
    return {
      intent: cancelled ? 'interview_cancelled' : 'interview_invite',
      // "Every .ics invite from a company domain is a near-certain interview."
      confidence: cancelled ? 0.95 : 0.97,
      stageHint: stage,
      company: companyFromOrganiser(inv.organiser, ctx.atsDomains),
      startsAt: inv.starts_at,
      endsAt: inv.ends_at,
      calendarEventId: inv.uid,
      applicationId: msg.thread_id ? (ctx.threadToApplication.get(msg.thread_id) ?? null) : null,
      location: inv.location,
    };
  }

  // ── a reply on a thread we already own ───────────────────────────────────
  // No parsing at all: the thread identity is the strongest and cheapest signal
  // in the system, so this abstains on *intent* and only asserts identity.
  if (msg.thread_id && ctx.threadToApplication.has(msg.thread_id) && !isAts) {
    return {
      intent: 'other',
      confidence: 0.99,
      stageHint: null,
      company: null,
      startsAt: null,
      endsAt: null,
      calendarEventId: null,
      applicationId: ctx.threadToApplication.get(msg.thread_id)!,
      location: null,
    };
  }

  return null;
}

function companyFromOrganiser(organiser: string | null, atsDomains: readonly string[]): string | null {
  const domain = organiser ? domainOfAddress(organiser) : null;
  if (!domain) return null;
  if (atsDomains.some((d) => matchesDomainSuffix(domain, d))) return null;
  const parts = domain.split('.');
  // "talent.nexi.it" → "Nexi". The resolver canonicalises this against the
  // company table anyway, but the fallback is sometimes the row that gets
  // created first — and a company called "nexi" in the interface looks like a
  // bug even when the matching is right.
  const label = parts.length >= 2 ? parts[parts.length - 2] : null;
  if (!label) return null;
  return label
    .split('-')
    .map((w) => (w ? w[0]!.toUpperCase() + w.slice(1) : w))
    .join(' ');
}
