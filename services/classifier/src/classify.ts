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
 * Two vocabularies, not one.
 *
 * The original single regex included "selezione", "posizione" and "offerta",
 * which in Italian are only job words in a job context: a fashion retailer's
 * "la selezione in saldo", an estate agent's "posizione centrale" and any
 * shop's "offerta" all matched it. Two thirds of everything that reached the
 * extraction ladder in a real twelve-month mailbox was mail of exactly that
 * kind.
 *
 * So the unambiguous words score on their own, and the ambiguous ones only
 * count when something else already suggests this is about work. Recall is
 * still the bias — a weak word plus any other signal is enough — but a weak
 * word alone no longer is.
 */
const STRONG_KEYWORDS =
  /candidatur|candidacy|\bapplication\b|applying|colloqui|interview|recruit|assunzione|hiring|\bcurriculum\b|\bCV\b|risorse umane|talent acquisition|job offer|offerta di lavoro|proposta di assunzione|processo di selezione/i;

const WEAK_KEYWORDS =
  /posizione|selezione|offerta|\brole\b|vacancy|talent|opportunit/i;

/**
 * Mail from a job platform that is not about a job.
 *
 * LinkedIn is whitelisted past the bulk penalty because its application
 * confirmations are bulk-flagged exactly like its alerts — but that waiver was
 * applied to everything it sends, so profile views, invitation accepts,
 * birthday nudges and security notices all sailed through. In this mailbox that
 * was 186 messages, every one of which then became a review item asking a human
 * to classify "your profile appeared in 8 searches".
 *
 * These are the shapes that are never an application, in both languages.
 */
const PLATFORM_NOISE =
  new RegExp(
    [
      // profile / network activity
      'profilo è apparso', 'appeared in \\d+ search', 'persone ti hanno notato',
      'ha accettato il tuo invito', 'accepted your invitation',
      'inizia una conversazione con', 'hanno aggiornamenti per te',
      'fai le congratulazioni', 'congratulate', 'ha aggiunto una reazione',
      'vedi i collegamenti', 'vorrei collegarmi', 'voglio collegarmi',
      'hai \\d+ nuov(?:o|i) (?:invito|inviti|messaggi)', 'nuovo invito',
      'sent you a message', 'ti ha inviato un messaggio',
      // alerts and account admin
      'avvisi? di offerte di lavoro', 'job alert', 'abbiamo disattivato',
      'sblocca informazioni', 'verifica il tuo nuovo dispositivo',
      'livello di protezione', 'two[- ]factor', 'autenticazione a due fattori',
      'terms of service', 'termini di servizio', "condizioni d'uso",
      'newsletter', 'webinar', 'unsubscribe preferences',
      // marketplace noise that borrows the vocabulary
      'saldi', 'in saldo', 'spedizione gratuita', 'sconto',
    ].join('|'),
    'i',
  );

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
  const strongHit = STRONG_KEYWORDS.test(haystack);
  const weakHit = !strongHit && WEAK_KEYWORDS.test(haystack);
  // Noise is judged on the subject alone: a LinkedIn footer mentions searches
  // and alerts on every message it ever sends, including the real ones.
  const noise = PLATFORM_NOISE.test(msg.headers.subject);

  const isAts = inList(senderDomain, ctx.atsDomains);
  const isKnownCompany =
    !msg.headers.list_unsubscribe && !!senderDomain && ctx.companyDomains.has(senderDomain);
  const onKnownThread = !!msg.thread_id && ctx.knownThreads.has(msg.thread_id);
  const hasMeetingLink =
    MEETING_HOSTS.some((h) => msg.text.includes(h)) && !inList(senderDomain, PERSONAL_MAIL);
  const hasInvite = !!msg.invite || hasMeetingLink;

  if (isAts) add(3, 'sender is a known ATS vendor');
  if (isKnownCompany) add(3, 'direct mail from a company already in the pipeline');

  if (strongHit) add(2, 'subject or opening names an application unambiguously');
  else if (weakHit && (isAts || isKnownCompany || onKnownThread || hasInvite)) {
    add(2, 'ambiguous vocabulary, corroborated by another signal');
  } else if (weakHit) {
    add(1, 'ambiguous vocabulary alone — enough to look at, not enough to trust');
  }
  const keywordHit = strongHit || weakHit;

  if (onKnownThread) add(2, 'reply on a thread already attached to an application');

  if (hasInvite) add(2, msg.invite ? 'carries a calendar invite' : 'meeting link from a business domain');

  // ── penalties ────────────────────────────────────────────────────────────
  const whitelisted = inList(senderDomain, BULK_WHITELIST);
  const bulk =
    /bulk|list/i.test(msg.headers.precedence ?? '') ||
    !!msg.headers.list_id ||
    (!!senderDomain && ctx.knownNewsletters.has(senderDomain));

  // The waiver is for their *confirmations*, which is what §07 says. Extending
  // it to every notification the platform emits is what buried the review queue.
  const waived = whitelisted && !noise;
  if (bulk && !waived) add(-4, 'bulk mail');
  else if (bulk && waived) {
    reasons.push('bulk penalty waived: LinkedIn/Indeed confirmations look exactly like their alerts');
  }

  // Platform housekeeping is never an application, whoever sent it.
  if (noise) add(-4, 'platform notification, not an application');

  const noReply = /^(no[-._]?reply|do[-._]?not[-._]?reply|noreply)@/i.test(msg.headers.from.replace(/^.*</, ''));
  if (noReply && !keywordHit) add(-3, 'no-reply sender with no vocabulary hit');

  if (inList(senderDomain, SOCIAL_NOISE)) add(-2, 'social or developer notification');

  const outcome: Classification['outcome'] =
    score >= CLASSIFIER.PASS ? 'pass' : score >= CLASSIFIER.CHEAP_ONLY ? 'cheap_only' : 'drop';

  return { score, outcome, reasons };
}
