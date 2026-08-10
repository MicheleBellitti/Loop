import type { AppStatus } from './types.js';
import { NOTIFICATIONS } from './thresholds.js';

/**
 * Row flags and quiet counters.
 *
 * The README is explicit that the client "never computes a statistic, never
 * derives a stage, and never decides whether an application is dormant — all of
 * that arrives precomputed, including `days_quiet` and each row's `flag`". The
 * prototypes show three different flag strings on the same column and never say
 * what happens when two apply at once, so the precedence is defined here:
 * soonest irreversible cost first. decisions.md C2.
 */

/** "Last signal" turns accent past this many days on the desktop table. */
export const LAST_SIGNAL_EMPHASIS_DAYS = 13;

export interface FlagInput {
  now: Date;
  status: AppStatus;
  /** Nearest unmet deadline from a `deadline_set` event, if any. */
  deadlineAt?: Date | null;
  /** From an offer's `decide_by`, if any. */
  decideBy?: Date | null;
  lastSignalAt?: Date | null;
  /** The threshold the dormancy job would use for this application's stage. */
  quietThresholdDays?: number | null;
  tz: string;
  locale?: string;
}

export type FlagKind = 'deadline' | 'decide' | 'quiet' | 'none';

export interface Flag {
  kind: FlagKind;
  text: string;
}

const DAY_MS = 86_400_000;
const DEADLINE_FLAG_WINDOW_HOURS = NOTIFICATIONS.DEADLINE_FLAG_WINDOW_DAYS * 24;

export function daysQuiet(now: Date, lastSignalAt: Date | null | undefined): number | null {
  if (!lastSignalAt) return null;
  return Math.max(0, Math.floor((now.getTime() - lastSignalAt.getTime()) / DAY_MS));
}

/** "1 day" / "6 days" — the pipeline row's meta line. */
export function quietLabel(days: number | null): string {
  if (days === null) return '';
  return days === 1 ? 'quiet 1 day' : `quiet ${days} days`;
}

function weekdayTime(d: Date, tz: string, locale: string): string {
  const day = new Intl.DateTimeFormat(locale, { weekday: 'long', timeZone: tz }).format(d);
  const time = new Intl.DateTimeFormat(locale, {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZone: tz,
  }).format(d);
  return `${day} ${time}`;
}

function dayMonth(d: Date, tz: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, { day: 'numeric', month: 'short', timeZone: tz }).format(d);
}

/**
 * One value per row, first match wins.
 *
 *   1. a take-home deadline inside a week — the only cost you cannot undo
 *   2. an offer you owe an answer to
 *   3. silence past your own p90 for this stage
 *
 * A closed application never carries a flag: it has nothing left to be late for.
 */
export function computeFlag(input: FlagInput): Flag {
  const locale = input.locale ?? 'en-GB';
  const { now } = input;

  if (input.status === 'rejected' || input.status === 'withdrawn' || input.status === 'accepted') {
    return { kind: 'none', text: '' };
  }

  if (input.deadlineAt) {
    const hours = (input.deadlineAt.getTime() - now.getTime()) / 3_600_000;
    if (hours > 0 && hours <= DEADLINE_FLAG_WINDOW_HOURS) {
      return { kind: 'deadline', text: `Due ${weekdayTime(input.deadlineAt, input.tz, locale)}` };
    }
  }

  if (input.decideBy && input.decideBy.getTime() > now.getTime()) {
    return { kind: 'decide', text: `decide by ${dayMonth(input.decideBy, input.tz, locale)}` };
  }

  const quiet = daysQuiet(now, input.lastSignalAt);
  const threshold = input.quietThresholdDays ?? null;
  if (input.status === 'dormant' || (quiet !== null && threshold !== null && quiet > threshold)) {
    return { kind: 'quiet', text: 'quiet · past your p90' };
  }

  return { kind: 'none', text: '' };
}
