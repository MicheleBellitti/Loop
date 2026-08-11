import {
  excerpt,
  matchesDomainSuffix,
  normaliseRole,
  RESOLVER,
  type CalendarInvite,
  type CandidateMessage,
  type Channel,
  type Intent,
  type Rung,
  type Signal,
} from '@loop/domain';
import { roleFromBody } from '@loop/rules';

/**
 * Turning a rung's answer into the one shape the resolver accepts.
 *
 * A signal is "one extracted, structured observation from one source message —
 * not yet attached to an application", so nothing here decides identity. That
 * is the resolver's job, and keeping the boundary sharp is what stops two
 * components from both half-guessing which application a message belongs to.
 */

/** The stage an intent implies when nothing more specific was extracted. */
export function stageForIntent(intent: Intent): string | null {
  switch (intent) {
    case 'applied':
      return 'applied';
    case 'acknowledged':
      return 'acknowledged';
    case 'schedule_screening':
      return 'recruiter_reachout';
    case 'interview_invite':
      return 'technical';
    case 'take_home':
      return 'take_home';
    case 'offer':
      return 'offer';
    case 'negotiation':
      return 'negotiating';
    default:
      return null;
  }
}

/**
 * Channel, attributed from the sender.
 *
 * `referral` is never inferred: a referral is something a human tells us, and
 * guessing one would quietly flatter the channel statistics the whole feature
 * exists to keep honest.
 */
export function channelForVendor(vendor: string | null, senderDomain: string | null): Channel | null {
  if (vendor === 'linkedin') return 'linkedin';
  if (vendor === 'indeed') return 'indeed';
  if (vendor) return 'career_page'; // an ATS means you applied on their site
  if (senderDomain && !matchesDomainSuffix(senderDomain, 'gmail.com')) return 'recruiter';
  return null;
}

const POSTING_URL = /https?:\/\/[^\s<>"')]*\/(jobs?|careers?|posizioni|vacanc\w*|opportunit\w*)\/[^\s<>"')]*/i;

export interface BuildSignalInput {
  msg: CandidateMessage;
  intent: Intent;
  confidence: number;
  rung: Rung;
  company: string | null;
  role: string | null;
  stageHint: string | null;
  deadline: string | null;
  comp: Signal['comp'];
  decideBy: string | null;
  vendor: string | null;
  channel: Channel | null;
  senderDomain: string | null;
  language: 'it' | 'en' | 'other';
  invite: CalendarInvite | null;
  applicationHint: string | null;
}

export function buildSignal(input: BuildSignalInput): Signal & { application_hint: string | null } {
  const { msg } = input;
  // Whatever rung produced this signal, the job title may still be sitting in
  // the body — a calendar invite says "Interview with Prima" in its subject and
  // names the role in the description. Recovering it here means every rung
  // benefits, and the resolver gets something to match on instead of the
  // placeholder that made every roleless signal look like a new application.
  const role = input.role ?? roleFromBody(`${msg.headers.subject}\n${msg.text}`);
  const normalised = role ? normaliseRole(role) : null;

  // A calendar invite is the most accurate `occurred_at` in the mailbox: the
  // event happened when the meeting is, not when the mail arrived.
  const occurredAt =
    input.invite?.starts_at ??
    (input.intent === 'interview_invite' ? msg.received_at : msg.received_at);

  const postingUrl = POSTING_URL.exec(msg.text)?.[0] ?? null;

  return {
    user_id: msg.user_id,
    mailbox_id: msg.mailbox_id,
    provider_message_id: msg.provider_message_id,
    thread_id: msg.thread_id,
    evidence_ref: msg.provider_message_id,
    sender_domain: input.senderDomain,
    intent: input.intent,
    company: input.company,
    // The original, because "Senior Backend Engineer" is what the user applied
    // for and what they will recognise in a list. The normalised form travels
    // beside it for the resolver to embed — conflating the two put lower-cased
    // strings on screen.
    role,
    role_normalised: normalised?.role ?? null,
    stage_hint: input.stageHint,
    occurred_at: occurredAt,
    deadline: input.deadline,
    comp: input.comp,
    decide_by: input.decideBy,
    language: input.language,
    confidence: input.confidence,
    rung: input.rung,
    ats_vendor: input.vendor,
    channel: input.channel,
    posting_url: postingUrl,
    location: normalised?.location ?? null,
    work_mode: normalised?.workMode ?? null,
    invite: input.invite,
    // Only ever set when this is heading somewhere a human will read it.
    excerpt:
      input.confidence < RESOLVER.REVIEW_BELOW
        ? excerpt(`"${msg.text}" — ${msg.headers.from}`)
        : null,
    application_hint: input.applicationHint,
  };
}
