import { domainOfAddress, matchesDomainSuffix, CLASSIFIER, type RawMessage } from '@loop/domain';

/**
 * "A cheap, deterministic filter whose only job is to protect the expensive
 * rungs. It MUST be biased towards recall: dropping a real application is
 * invisible and unrecoverable, while passing junk through costs 4 ms at rung 1."
 *
 * Every branch below is one line of Engineering Spec §07, kept in the same
 * order so the two can be read side by side.
 */

export interface ClassifierContext {
  /** Sending domains of the ATS vendors in rules/ats/*.yaml. */
  atsDomains: readonly string[];
  /** Domains of companies this user already has an application at. */
  companyDomains: ReadonlySet<string>;
  /** Threads already attached to an application. */
  knownThreads: ReadonlySet<string>;
  /** Newsletter senders learned from previous drops. */
  knownNewsletters: ReadonlySet<string>;
}

export interface Classification {
  score: number;
  outcome: 'pass' | 'cheap_only' | 'drop';
  reasons: string[];
}

/**
 * Italian and English. A separate `it:` vocabulary rather than one regex with
 * both, because the two languages disagree about which words are generic:
 * "posizione" is specific, "position" is not.
 */
const KEYWORDS =
  /candidat|applicat|posizione|colloquio|interview|recruit|assunzione|offerta|hiring|role|vacancy|selezione|risorse umane|talent/i;

/**
 * Bulk-flagged but relevant. "This is the single most common false-negative in
 * the whole system; there is a fixture for it." Their confirmations carry
 * List-Unsubscribe and Precedence: bulk exactly like their job alerts do, so
 * the penalty is waived before it applies rather than compensated afterwards.
 */
const BULK_WHITELIST = [
  'linkedin.com',
  'e.linkedin.com',
  'bounce.linkedin.com',
  'indeed.com',
  'match.indeed.com',
  'indeedemail.com',
];

const SOCIAL_NOISE = [
  'facebook.com', 'facebookmail.com', 'twitter.com', 'x.com', 'instagram.com',
  'github.com', 'gitlab.com', 'notifications.google.com', 'youtube.com',
  'medium.com', 'substack.com', 'meetup.com', 'slack.com', 'discord.com',
  'reddit.com', 'quora.com', 'pinterest.com', 'tiktok.com',
];

const MEETING_HOSTS = [
  'meet.google.com', 'zoom.us', 'teams.microsoft.com', 'teams.live.com',
  'whereby.com', 'meet.jit.si', 'webex.com', 'gotomeeting.com', 'around.co',
];

const PERSONAL_MAIL = [
  'gmail.com', 'googlemail.com', 'outlook.com', 'hotmail.com', 'hotmail.it',
  'live.com', 'yahoo.com', 'yahoo.it', 'icloud.com', 'me.com', 'proton.me',
  'protonmail.com', 'libero.it', 'virgilio.it', 'alice.it', 'tiscali.it',
  'fastwebnet.it', 'tin.it', 'gmx.de', 'web.de',
];

const inList = (domain: string | null, list: readonly string[]): boolean =>
  !!domain && list.some((c) => matchesDomainSuffix(domain, c));

export function classify(msg: RawMessage, ctx: ClassifierContext): Classification {
  const reasons: string[] = [];
  let score = 0;
  const add = (points: number, why: string): void => {
    score += points;
    reasons.push(`${points > 0 ? '+' : ''}${points} ${why}`);
  };

  const senderDomain = domainOfAddress(msg.headers.from);
  const haystack = `${msg.headers.subject}\n${msg.text.slice(0, 400)}`;
  const keywordHit = KEYWORDS.test(haystack);

  if (inList(senderDomain, ctx.atsDomains)) add(3, 'sender is a known ATS vendor');

  if (!msg.headers.list_unsubscribe && senderDomain && ctx.companyDomains.has(senderDomain)) {
    add(3, 'direct mail from a company already in the pipeline');
  }

  if (keywordHit) add(2, 'subject or opening matches the vocabulary');

  if (msg.thread_id && ctx.knownThreads.has(msg.thread_id)) {
    add(2, 'reply on a thread already attached to an application');
  }

  const hasMeetingLink =
    MEETING_HOSTS.some((h) => msg.text.includes(h)) && !inList(senderDomain, PERSONAL_MAIL);
  if (msg.invite || hasMeetingLink) add(2, msg.invite ? 'carries a calendar invite' : 'meeting link from a business domain');

  // ── penalties ────────────────────────────────────────────────────────────
  const whitelisted = inList(senderDomain, BULK_WHITELIST);
  const bulk =
    /bulk|list/i.test(msg.headers.precedence ?? '') ||
    !!msg.headers.list_id ||
    (!!senderDomain && ctx.knownNewsletters.has(senderDomain));

  if (bulk && !whitelisted) add(-4, 'bulk mail');
  else if (bulk && whitelisted) reasons.push('bulk penalty waived: LinkedIn/Indeed confirmations look exactly like their alerts');

  const noReply = /^(no[-._]?reply|do[-._]?not[-._]?reply|noreply)@/i.test(msg.headers.from.replace(/^.*</, ''));
  if (noReply && !keywordHit) add(-3, 'no-reply sender with no vocabulary hit');

  if (inList(senderDomain, SOCIAL_NOISE)) add(-2, 'social or developer notification');

  const outcome: Classification['outcome'] =
    score >= CLASSIFIER.PASS ? 'pass' : score >= CLASSIFIER.CHEAP_ONLY ? 'cheap_only' : 'drop';

  return { score, outcome, reasons };
}
