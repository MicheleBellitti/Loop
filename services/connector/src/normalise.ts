import { createHash } from 'node:crypto';
import {
  normaliseMessage,
  type CalendarInvite,
  type MessageHeaders,
  type RawMessage,
} from '@loop/domain';
import type { GmailMessage, GmailMessagePart } from '@loop/google';

/**
 * A Gmail message → the one shape the rest of the pipeline sees.
 *
 * Normalisation happens here, in the only process that ever holds the raw
 * payload, so the text that travels between stages is already the reduced,
 * de-quoted, capped version. §08 places pre-processing in the extractor; doing
 * it once at the edge means the classifier scores the same text the extractor
 * reads, which is the only way the two can be reasoned about together.
 */

function decode(data: string | undefined): string {
  if (!data) return '';
  return Buffer.from(data.replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString('utf8');
}

function headerMap(part: GmailMessagePart | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const h of part?.headers ?? []) out[h.name.toLowerCase()] = h.value;
  return out;
}

function collectParts(part: GmailMessagePart | undefined, acc: GmailMessagePart[] = []): GmailMessagePart[] {
  if (!part) return acc;
  acc.push(part);
  for (const p of part.parts ?? []) collectParts(p, acc);
  return acc;
}

function pickBody(parts: GmailMessagePart[]): { text?: string; html?: string } {
  let text: string | undefined;
  let html: string | undefined;
  for (const p of parts) {
    if (p.filename) continue;
    if (p.mimeType === 'text/plain' && !text) text = decode(p.body?.data);
    if (p.mimeType === 'text/html' && !html) html = decode(p.body?.data);
  }
  return { text, html };
}

/**
 * The .ics parse.
 *
 * Deliberately small: only the seven fields a stage decision needs. An invite
 * is "the cheapest stage detector that exists" precisely because it does not
 * require understanding the calendar format, only reading it.
 */
export function parseIcs(ics: string): CalendarInvite | null {
  const unfolded = ics.replace(/\r?\n[ \t]/g, '');
  const get = (key: string): string | null => {
    const m = new RegExp(`^${key}(?:;[^:]*)?:(.*)$`, 'im').exec(unfolded);
    return m ? m[1]!.trim() : null;
  };
  const uid = get('UID');
  const dtstart = get('DTSTART');
  if (!uid || !dtstart) return null;

  const toIso = (raw: string | null): string | null => {
    if (!raw) return null;
    const m = /^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})(Z)?)?$/.exec(raw);
    if (!m) return new Date(raw).toISOString();
    const [, y, mo, d, h = '00', mi = '00', s = '00'] = m;
    return new Date(`${y}-${mo}-${d}T${h}:${mi}:${s}${m[7] ? 'Z' : 'Z'}`).toISOString();
  };

  const method = get('METHOD') as CalendarInvite['method'];
  const status = (get('STATUS') ?? 'CONFIRMED').toLowerCase();
  const attendees = [...unfolded.matchAll(/^ATTENDEE(?:;[^:]*)?:mailto:(.+)$/gim)].map((m) => m[1]!.trim());

  return {
    uid,
    summary: get('SUMMARY'),
    starts_at: toIso(dtstart)!,
    ends_at: toIso(get('DTEND')),
    location: get('LOCATION'),
    organiser: get('ORGANIZER')?.replace(/^mailto:/i, '') ?? null,
    attendees,
    status: status === 'cancelled' ? 'cancelled' : status === 'tentative' ? 'tentative' : 'confirmed',
    method: method ?? null,
  };
}

export function toRawMessage(
  gmail: GmailMessage,
  ctx: { userId: string; mailboxId: string; backfill?: boolean },
): RawMessage {
  const parts = collectParts(gmail.payload);
  const h = headerMap(gmail.payload);
  const { text, html } = pickBody(parts);
  const normalised = normaliseMessage({ text, html });

  const icsPart = parts.find(
    (p) => p.mimeType === 'text/calendar' || /\.ics$/i.test(p.filename ?? ''),
  );
  const invite = icsPart?.body?.data ? parseIcs(decode(icsPart.body.data)) : null;

  const headers: MessageHeaders = {
    message_id: h['message-id'] ?? gmail.id,
    from: h.from ?? '',
    to: (h.to ?? '').split(',').map((s) => s.trim()).filter(Boolean),
    subject: h.subject ?? '',
    date: h.date ?? new Date(Number(gmail.internalDate ?? Date.now())).toISOString(),
    in_reply_to: h['in-reply-to'] ?? null,
    references: (h.references ?? '').split(/\s+/).filter(Boolean),
    list_id: h['list-id'] ?? null,
    list_unsubscribe: h['list-unsubscribe'] ?? null,
    precedence: h.precedence ?? null,
    auto_submitted: h['auto-submitted'] ?? null,
  };

  return {
    user_id: ctx.userId,
    mailbox_id: ctx.mailboxId,
    provider_message_id: gmail.id,
    thread_id: gmail.threadId ?? null,
    received_at: new Date(Number(gmail.internalDate ?? Date.now())).toISOString(),
    headers,
    text: normalised.text,
    // Hashing the *normalised* text, not the wire bytes: a message forwarded to
    // yourself or re-delivered with different transport headers is the same
    // message, and the replay log should say so.
    body_sha256: createHash('sha256').update(normalised.text).digest('hex'),
    invite,
    backfill: ctx.backfill ?? false,
  };
}
