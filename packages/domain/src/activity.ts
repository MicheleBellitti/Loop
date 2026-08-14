import { SILENCE } from './thresholds.js';

/**
 * Is this application still happening?
 *
 * `status` cannot answer that on its own. It is `live` from the moment an
 * application is created until something — a rejection, the nightly sweep, a
 * human — moves it, so a mailbox that has been read for a year accumulates
 * dozens of `live` rows nobody has thought about since spring. Counting those
 * as your pipeline is the difference between "you have fourteen applications
 * open" and the truth, which was four.
 *
 * Worse, it poisons every ratio on the statistics page: the display gates count
 * *closed* applications, so processes that are over but were never marked over
 * hold the denominator below the gate and the page shows an em dash next to a
 * funnel that plainly has numbers in it. That is the bug this file exists to
 * fix, and fixing it in one place is why the same three states are used by the
 * board, the counters and the metrics.
 *
 * Three states, and the order of the rules is the whole of the definition:
 *
 *   closed — over, whatever the column says. An explicit outcome, the sweep's
 *            `presumed_closed`, or silence long enough to mean it.
 *   stale  — quiet past the threshold for its stage, not yet long enough to
 *            write off. This is what a follow-up is for.
 *   active — moving, or waiting on you, or with a date in the calendar.
 *
 * The SQL that decides the same question for a whole table is in the gateway,
 * built from these same constants, and `activity.test.ts` pins the ladder.
 */

export type Activity = 'active' | 'stale' | 'closed';

export interface ActivityInput {
  now: Date;
  /** The projected status: live, dormant, rejected, withdrawn, accepted. */
  status: string;
  currentStage: string;
  currentPhase: string;
  /** The sweep's second-tier judgement, folded onto the row. */
  presumedClosed?: boolean;
  lastSignalAt?: Date | null;
  /** The next uncancelled interview in the future, if one is scheduled. */
  nextInterviewAt?: Date | null;
  /** `stale_after_days` for the stage, or twice the user's own p90 for it. */
  quietThresholdDays?: number | null;
}

const DAY_MS = 86_400_000;
/** What the dormancy sweep falls back to when a stage names no staleness. */
const DEFAULT_STALE_AFTER_DAYS = 21;

export function activityOf(input: ActivityInput): Activity {
  // An outcome that was actually recorded outranks every inference below it.
  if (input.status !== 'live') return 'closed';

  // A date in the calendar is the strongest evidence there is that something is
  // still happening, and it outranks silence: an interview booked six weeks out
  // leaves a long quiet gap that means the opposite of what quiet usually means.
  if (input.nextInterviewAt && input.nextInterviewAt.getTime() > input.now.getTime()) return 'active';

  if (input.presumedClosed) return 'closed';

  // The ball is in your court. Silence here is a task, not a verdict — the same
  // exemption the dormancy sweep makes, for the same reason.
  if (SILENCE.SKIP_STAGES.includes(input.currentStage)) return 'active';

  // Nothing has ever arrived on this row. A manual add on the day it is made
  // reads as active rather than as silent, which is what it is.
  if (!input.lastSignalAt) return 'active';

  const quiet = (input.now.getTime() - input.lastSignalAt.getTime()) / DAY_MS;
  if (quiet > closureDays(input.currentPhase)) return 'closed';

  const threshold = input.quietThresholdDays ?? DEFAULT_STALE_AFTER_DAYS;
  return quiet > threshold ? 'stale' : 'active';
}

/**
 * How long silence has to run before it is a decision rather than a delay.
 *
 * Shorter for an application still in the `sent` phase — applied, or
 * acknowledged by a robot and nothing since. There is no panel to convene and
 * no calendar to fit; two months of that is a no that nobody typed.
 */
export function closureDays(currentPhase: string): number {
  return currentPhase === 'sent' ? SILENCE.NO_REPLY_CLOSED_DAYS : SILENCE.PRESUMED_CLOSED_DAYS;
}

/** Open is what the board shows by default: everything that is not over. */
export function isOpen(activity: Activity): boolean {
  return activity !== 'closed';
}
