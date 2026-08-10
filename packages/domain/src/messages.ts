import type { Channel, EventType, EventPayload, Rung } from './types.js';

/**
 * The queue contracts. Shared types are the reason §02 puts one language on
 * both sides of the wire.
 *
 * ── Where message bodies live ──────────────────────────────────────────────
 *
 * "No table ever stores message bodies" (Spec §04) and "one queue per stage of
 * the pipeline" (§02) are in tension, because pgmq is a table. The resolution,
 * recorded in decisions.md C11:
 *
 *   · text travels in the queue while a message is in flight, and the ack
 *     deletes the row — this is the "lives in memory for one parse" window,
 *     extended by the seconds a queue hop takes;
 *   · the dead-letter path strips it, because that is the only path where a
 *     payload would persist indefinitely. Nothing is lost: `seen_messages`
 *     makes every message replayable from the provider by id, which is what
 *     the replay log is for.
 */

export interface MessageHeaders {
  message_id: string;
  from: string;
  to: string[];
  subject: string;
  date: string;
  in_reply_to?: string | null;
  references?: string[];
  list_id?: string | null;
  list_unsubscribe?: string | null;
  precedence?: string | null;
  auto_submitted?: string | null;
}

export interface CalendarInvite {
  uid: string;
  summary: string | null;
  starts_at: string;
  ends_at: string | null;
  location: string | null;
  organiser: string | null;
  attendees: string[];
  status: 'confirmed' | 'cancelled' | 'tentative';
  method: 'REQUEST' | 'CANCEL' | 'REPLY' | 'PUBLISH' | null;
}

/** connector → classifier */
export interface RawMessage {
  user_id: string;
  mailbox_id: string;
  provider_message_id: string;
  thread_id: string | null;
  received_at: string;
  headers: MessageHeaders;
  /** HTML already reduced to text, quoted history dropped, capped at 6 000. */
  text: string;
  body_sha256: string;
  invite: CalendarInvite | null;
  /** True when this arrived from a backfill rather than a live push. */
  backfill?: boolean;
}

/** classifier → extractor */
export interface CandidateMessage extends RawMessage {
  score: number;
  /** Score 1–2: rungs 1 and 2 only, never the model. */
  cheap_only: boolean;
  reasons: string[];
}

export type Intent =
  | 'applied'
  | 'acknowledged'
  | 'schedule_screening'
  | 'interview_invite'
  | 'interview_cancelled'
  | 'take_home'
  | 'rejected'
  | 'offer'
  | 'negotiation'
  | 'other'
  | 'unclear';

/** extractor → resolver. One extracted, structured observation. */
export interface Signal {
  user_id: string;
  mailbox_id: string;
  provider_message_id: string;
  thread_id: string | null;
  evidence_ref: string;
  sender_domain: string | null;
  intent: Intent;
  company: string | null;
  /** As written in the message — this is what the interface shows. */
  role: string | null;
  /** Seniority lifted out, abbreviations expanded, location stripped. The
   *  comparison key the resolver embeds, never the display string. */
  role_normalised: string | null;
  stage_hint: string | null;
  occurred_at: string;
  deadline: string | null;
  comp: { min_minor?: number; max_minor?: number | null; currency: string } | null;
  decide_by?: string | null;
  language: 'it' | 'en' | 'other';
  confidence: number;
  rung: Rung;
  ats_vendor: string | null;
  channel: Channel | null;
  posting_url: string | null;
  location: string | null;
  work_mode: 'onsite' | 'hybrid' | 'remote' | null;
  invite: CalendarInvite | null;
  /** ≤280 chars, redacted. Only ever set when this is heading for review. */
  excerpt: string | null;
}

/** resolver → pipeline. The only shape that becomes an event. */
export interface PendingEvent {
  user_id: string;
  application_id: string;
  event: {
    type: EventType;
    occurred_at: string;
    confidence: number;
    from_stage?: string | null;
    to_stage?: string | null;
    payload?: EventPayload;
    evidence_ref?: string | null;
    rung?: Rung | null;
  };
  /** A provenance row to attach, when this signal introduced a new channel. */
  source?: {
    channel: Channel;
    posting_url?: string | null;
    ats_vendor?: string | null;
    is_first_touch?: boolean;
  };
  /** Set on replay so the pipeline can skip re-notifying. */
  silent?: boolean;
}

/** nudge → notifier */
export interface PendingNotification {
  user_id: string;
  suggestion_key: string;
  rule: string;
  title: string;
  body: string;
  url: string;
  /** Only the deadline rule sets this. */
  bypasses_budget: boolean;
}

/** Keys stripped before a payload is dead-lettered. */
export const BODY_KEYS = ['text', 'body', 'html', 'snippet', 'excerpt'] as const;

export function stripBodies<T>(payload: T): T {
  const walk = (node: unknown): unknown => {
    if (Array.isArray(node)) return node.map(walk);
    if (node && typeof node === 'object') {
      const out: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
        out[k] = (BODY_KEYS as readonly string[]).includes(k) ? '[stripped]' : walk(v);
      }
      return out;
    }
    return node;
  };
  return walk(payload) as T;
}
