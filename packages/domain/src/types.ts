/**
 * The vocabulary of the system. Everything else in this package is a pure
 * function over these shapes; nothing here touches I/O.
 */

/** The unit every statistic aggregates on (Engineering Spec §01, §06). */
export type Phase = 'sent' | 'screening' | 'interviewing' | 'decided';

/** A state, never a stage. `dormant` is reversible; the other three are not. */
export type AppStatus = 'live' | 'dormant' | 'rejected' | 'withdrawn' | 'accepted';

/** How the application was first found. `referral` is never folded into a board. */
export type Channel = 'linkedin' | 'indeed' | 'career_page' | 'referral' | 'recruiter' | 'other';

/** Which rung of the extraction ladder produced a claim. 4 is the human. */
export type Rung = 1 | 2 | 3 | 4;

/**
 * The closed set of event types.
 *
 * Fourteen, not thirteen: the Engineering Spec §05 table and the Architecture
 * sheet §05 list a different thirteen (`deadline_set` vs `note_added`), and the
 * `prepare` nudge promises to surface the user's notes, so notes must exist.
 * See docs/decisions.md A2. Adding another one is a migration plus a change to
 * the fold — never an ad-hoc string.
 */
export const EVENT_TYPES = [
  'applied',
  'acknowledged',
  'stage_advanced',
  'interview_scheduled',
  'interview_held',
  'deadline_set',
  'offer_received',
  'offer_negotiated',
  'rejected',
  'withdrawn',
  'accepted',
  'went_silent',
  'human_corrected',
  'note_added',
] as const;

export type EventType = (typeof EVENT_TYPES)[number];

/** Statuses an automated rung may not move away from (Spec §10). */
export const TERMINAL_STATUSES = ['rejected', 'withdrawn', 'accepted'] as const satisfies readonly AppStatus[];
export type TerminalStatus = (typeof TERMINAL_STATUSES)[number];

/**
 * Fields the fold can decide. Kept as a closed union so a correction can only
 * ever name a field the fold actually understands.
 */
export const CORRECTABLE_FIELDS = [
  'stage',
  'status',
  'role_title',
  'seniority',
  'location',
  'work_mode',
  'company_id',
  'channel',
  'applied_at',
  'comp_expectation',
  // Not a column: recorded when a human splits an automatic merge apart, so the
  // resolver can refuse to merge the same pair again.
  'merge',
] as const;
export type CorrectableField = (typeof CORRECTABLE_FIELDS)[number];

export interface Money {
  minor: number;
  currency: string; // ISO-4217, uppercase
}

/**
 * Descriptive fields any event may carry. They live in the payload rather than
 * on the application row so that `applications` stays a projection that can be
 * dropped and rebuilt from the log alone (Spec §04, invariant 6).
 */
export interface EventFields {
  role_title?: string | null;
  seniority?: string | null;
  location?: string | null;
  work_mode?: 'onsite' | 'hybrid' | 'remote' | null;
  company_id?: string | null;
  channel?: Channel | null;
  comp_expectation?: Money | null;
}

export interface DomainEvent {
  /** Stable identity used only for logging; never used to order the fold. */
  id?: string | number;
  type: EventType;
  /** When it happened in the world. */
  occurred_at: Date;
  /** When we learned it. Never affects the fold — only the UI's "picked up". */
  recorded_at?: Date;
  from_stage?: string | null;
  to_stage?: string | null;
  confidence: number;
  /** Provider message/event id. Never a body. Doubles as the fold's tie-break. */
  evidence_ref?: string | null;
  rung?: Rung | null;
  payload?: EventPayload;
}

export interface EventPayload extends EventFields {
  /** `human_corrected` only. */
  field?: CorrectableField;
  from?: unknown;
  to?: unknown;
  /** `interview_scheduled` */
  stage?: string;
  starts_at?: string;
  ends_at?: string | null;
  calendar_event_id?: string;
  /** `interview_held` */
  interview_id?: string;
  /** `deadline_set` */
  kind?: string;
  due_at?: string;
  url?: string | null;
  /** `offer_received` / `offer_negotiated` */
  min_minor?: number;
  max_minor?: number | null;
  currency?: string;
  decide_by?: string | null;
  equity_note?: string | null;
  /** `acknowledged` */
  ats_vendor?: string;
  /** `applied` */
  posting_url?: string | null;
  /** `rejected` */
  after_stage?: string | null;
  verbatim_hash?: string | null;
  /** `went_silent` */
  days_quiet?: number;
  threshold_used?: string;
  /** `withdrawn` */
  reason?: string | null;
  /** `accepted` */
  start_date?: string | null;
  /** `note_added` */
  text?: string;
  /** free-form, ignored by the fold */
  note?: string;
  [k: string]: unknown;
}

/** Everything the fold decides. One pure function of the event set. */
export interface ApplicationState {
  current_stage: string;
  current_phase: Phase;
  status: AppStatus;
  applied_at: Date | null;
  last_signal_at: Date | null;
  role_title: string | null;
  seniority: string | null;
  location: string | null;
  work_mode: 'onsite' | 'hybrid' | 'remote' | null;
  company_id: string | null;
  channel: Channel | null;
  comp_expectation_minor: number | null;
  comp_currency: string | null;
  /** Confidence of the event that decided the current stage. */
  confidence: number;
}

export interface StageDef {
  key: string;
  label: string;
  phase: Phase;
  /** Sort order and the "how far did it get" statistic. Never a gate. */
  depth: number;
  stale_after_days: number;
}
