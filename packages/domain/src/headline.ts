import type { DomainEvent } from './types.js';
import { DEFAULT_STAGES, StageTable } from './stages.js';

/**
 * The Today headline.
 *
 * The handoff is emphatic that this "is generated from the week's events — it is
 * the product's one encouraging gesture and must never be a static string", and
 * that when the week had no forward movement it must fall back to a neutral
 * statement of fact, "never to false cheer". It then gives no generation rule,
 * so here is one. decisions.md C1.
 *
 * Four outcomes, matching the four headlines drawn across the prototypes:
 *
 *   forward movement    → "Three moved / forward / this week"
 *   nothing tracked yet → "Nothing / to track yet"              (empty state E1)
 *   nothing needs you   → "You are / clear today"               (empty state E2)
 *   otherwise           → "Nine applications / waiting"
 *
 * The third case is the one worth defending: a pipeline that is full and quiet
 * is a good day, and saying so is not cheer — it is the accurate reading.
 */

export interface HeadlineInput {
  /** Events across all of the user's applications, any window. */
  events: readonly DomainEvent[];
  /** Application id per event, so "three moved" counts applications not events. */
  applicationIdOf: (ev: DomainEvent) => string;
  /** Count of applications with status `live`. */
  liveCount: number;
  /** Suggestions currently surfaced. Zero of them is what "clear" means. */
  openSuggestionCount: number;
  now: Date;
  windowDays?: number;
  stages?: StageTable;
}

export interface Headline {
  lines: string[];
  /** Which branch produced it — the client uses it for nothing, tests use it. */
  kind: 'moved' | 'empty' | 'clear' | 'waiting';
  /** Applications that moved forward in the window. */
  movedCount: number;
}

const WORDS = [
  'Zero', 'One', 'Two', 'Three', 'Four', 'Five', 'Six',
  'Seven', 'Eight', 'Nine', 'Ten', 'Eleven', 'Twelve',
];

/** Small numbers read better as words; past twelve, the numeral is clearer. */
export function numberWord(n: number): string {
  return WORDS[n] ?? String(n);
}

/**
 * Movement that counts as forward. A stage change that goes *backwards* — an
 * extra round was added — is legitimate and common, and reporting it as
 * progress would be exactly the false cheer the design forbids.
 */
export function isForwardEvent(ev: DomainEvent, stages: StageTable = DEFAULT_STAGES): boolean {
  switch (ev.type) {
    case 'interview_scheduled':
    case 'offer_received':
      return true;
    case 'stage_advanced': {
      const to = ev.to_stage ?? (typeof ev.payload?.to_stage === 'string' ? ev.payload.to_stage : null);
      if (!to) return false;
      return stages.isForward(ev.from_stage ?? null, to);
    }
    default:
      return false;
  }
}

export function buildHeadline(input: HeadlineInput): Headline {
  const stages = input.stages ?? DEFAULT_STAGES;
  const windowDays = input.windowDays ?? 7;
  const since = input.now.getTime() - windowDays * 86_400_000;

  const moved = new Set<string>();
  for (const ev of input.events) {
    if (ev.occurred_at.getTime() < since) continue;
    if (ev.occurred_at.getTime() > input.now.getTime()) continue;
    if (isForwardEvent(ev, stages)) moved.add(input.applicationIdOf(ev));
  }

  const n = moved.size;
  if (n > 0) {
    return { lines: [`${numberWord(n)} moved`, 'forward', 'this week'], kind: 'moved', movedCount: n };
  }
  if (input.liveCount === 0) {
    return { lines: ['Nothing', 'to track yet'], kind: 'empty', movedCount: 0 };
  }
  if (input.openSuggestionCount === 0) {
    return { lines: ['You are', 'clear today'], kind: 'clear', movedCount: 0 };
  }
  return {
    lines: [`${numberWord(input.liveCount)} applications`, 'waiting'],
    kind: 'waiting',
    movedCount: 0,
  };
}

/** "Thursday 30 July" — computed server-side, in the user's timezone. */
export function dateEyebrow(now: Date, tz: string, locale = 'en-GB'): string {
  return new Intl.DateTimeFormat(locale, {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    timeZone: tz,
  }).format(now);
}
