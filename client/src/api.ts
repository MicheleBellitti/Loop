/**
 * The API client.
 *
 * Every mutation carries the CSRF token the session issued; the client never
 * parses `error.message`, only `error.code`, which is the contract §13 states.
 */

export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

let csrf: string | null = null;

export function setCsrf(token: string | null): void {
  csrf = token;
}

export function getCsrf(): string | null {
  return csrf;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = init.method ?? 'GET';
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  if (method !== 'GET' && method !== 'HEAD') {
    // Only when there is something to declare. Announcing a JSON body and then
    // sending none is a 400 from Fastify before the handler ever runs — and
    // several mutations here legitimately have no body ("start the OAuth
    // dance", "archive this"), so this is the common case, not the edge one.
    if (init.body !== undefined) headers['content-type'] = 'application/json';
    if (csrf) headers['x-csrf-token'] = csrf;
  }

  const res = await fetch(path, { ...init, headers, credentials: 'same-origin' });
  if (res.status === 204) return undefined as T;

  const isJson = (res.headers.get('content-type') ?? '').includes('application/json');
  const body = isJson ? await res.json() : await res.text();

  if (!res.ok) {
    const err = (body as { error?: { code?: string; message?: string } }).error;
    throw new ApiError(err?.code ?? 'unknown', err?.message ?? 'request failed', res.status);
  }
  return body as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  del: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'DELETE', body: body === undefined ? undefined : JSON.stringify(body) }),
};

// ── shapes ─────────────────────────────────────────────────────────────────

export interface Counters {
  live: number;
  interviewing: number;
  offer: number;
  overdue: number;
}

export interface MailboxHealth {
  connected: boolean;
  providers: Array<{ provider: string; status: string; last_ok_at: string | null }>;
  last_ok_at: string | null;
  minutes_since_read: number | null;
  placed_today: number;
  backlog: number;
  state: 'ok' | 'F1' | 'F2';
}

export interface Suggestion {
  key: string;
  rule: string;
  kind: string;
  meta: string;
  title: string;
  body: string;
  cta: string;
  applicationIds: string[];
}

export interface Today {
  eyebrow: string;
  headline: string[];
  headline_kind: 'moved' | 'empty' | 'clear' | 'waiting';
  counters: Counters;
  review_count: number;
  next_interview: {
    application_id: string;
    company: string;
    role: string;
    stage: string;
    starts_at: string;
    rounds_done: number;
    provenance: string;
  } | null;
  suggestions: Suggestion[];
  recent_events: Array<{
    application_id: string;
    company: string;
    what: string;
    when: string;
    closed: boolean;
  }>;
  mailbox_health: MailboxHealth;
  closing_line: string;
}

export interface ApplicationRow {
  id: string;
  company: string;
  role: string;
  stage: string;
  display_stage: string;
  phase: string;
  status: string;
  channel: string | null;
  applied_at: string | null;
  last_signal_at: string | null;
  days_quiet: number | null;
  quiet_label: string;
  flag: string;
  flag_kind: string;
  closed: boolean;
  needs_review: boolean;
  confidence: number;
}

export interface ApplicationDetail extends ApplicationRow {
  facts: {
    applied: string | null;
    ats: string | null;
    posting_url: string | null;
    location: string | null;
    posted_range: { min_minor: string; max_minor: string | null; currency: string } | null;
    offers: Array<{ min_minor: string; currency: string }>;
  };
  events: Array<{
    id: string;
    when: string;
    what: string;
    detail: string;
    source: string;
    conf: string;
    rung: number | null;
  }>;
}

export interface Metric {
  label: string;
  value: number | null;
  numerator: number;
  denominator: number;
  excluded: number;
  gate_met: boolean;
  small_sample: boolean;
  note: string;
  display: string;
}

export interface Stats {
  period: '90d' | '12m' | 'all';
  funnel: Array<{ label: string; n: number; width: number }>;
  ratios: Metric[];
  first_response: { value: number | null; n: number; display: string; caption: string };
  ghost: Metric & { caption: string };
  channels: Array<{
    name: string;
    sent: number;
    gate_met: boolean;
    iv: string;
    of: string;
    ghost: string;
    note: string;
  }>;
  channel_note: string;
  time_in_stage: Array<{ stage: string; days: number; n: number; gate_met: boolean }>;
  compensation: {
    domain: { min: number; max: number; currency: string } | null;
    posted: Array<{ from: number; to: number; label: string }>;
    ask: { at: number } | null;
    offers: Array<{ at: number; label: string }>;
    currency: string;
    dropped: number;
  };
  seasonal: { gate_met: boolean; note: string };
}

export interface ReviewItem {
  id: string;
  kind: 'ambiguous_match' | 'unknown_intent' | 'low_confidence' | 'merge_undo';
  evidence_ref: string;
  excerpt: string | null;
  candidates: Array<{
    application_id: string;
    role_title: string;
    stage: string;
    applied_at: string | null;
    cosine: number;
  }>;
  application_id: string | null;
  created_at: string;
}

export interface Draft {
  subject: string;
  body: string;
  mailto_url: string;
  can_send: false;
  note: string;
}
