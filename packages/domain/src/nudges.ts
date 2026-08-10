import type { AppStatus } from './types.js';
import { DEFAULT_STAGES, StageTable } from './stages.js';
import { NOTIFICATIONS } from './thresholds.js';
import { daysBetween, hoursBetween, relativeFuture, relativePast } from './time.js';

/**
 * The four nudge rules, as a pure function of a snapshot.
 *
 * "A scheduled fold over the log, not a chat. Each rule produces at most one
 * suggestion per application, expires on its own, and is written so that doing
 * nothing is always acceptable."
 *
 * The service around this decides *when* to run and *whether* to push; this
 * decides *what* is true. Keeping it pure is what makes the budget testable.
 */

export type NudgeRule = 'deadline' | 'prepare' | 'follow_up_due' | 'let_it_go';

/** Urgency order for the "ranked by urgency then depth" rule (Spec §12). */
const RULE_URGENCY: Record<NudgeRule, number> = {
  deadline: 0,
  prepare: 1,
  follow_up_due: 2,
  let_it_go: 3,
};

export interface AppSnapshot {
  id: string;
  company: string;
  role_title: string | null;
  current_stage: string;
  status: AppStatus;
  last_signal_at: Date | null;
  /** True when the ball is in their court — set by the pipeline from the log. */
  awaiting_them: boolean;
  /** When the user last acted on this application (archive, correct, note). */
  last_user_action_at: Date | null;
  went_dormant_at: Date | null;
}

export interface InterviewSnapshot {
  id: string;
  application_id: string;
  stage: string;
  starts_at: Date;
}

export interface DeadlineSnapshot {
  application_id: string;
  kind: string;
  due_at: Date;
  source: string;
}

export interface NudgeInput {
  now: Date;
  applications: readonly AppSnapshot[];
  interviews: readonly InterviewSnapshot[];
  deadlines: readonly DeadlineSnapshot[];
  /** p75 dwell for a stage from the user's own history, null below the gate. */
  p75DwellDays: (stage: string) => number | null;
  /** p50, used only for the copy ("your median wait here is 3 days"). */
  p50DwellDays: (stage: string) => number | null;
  /**
   * Keys of suggestions already issued and not yet expired. One suggestion per
   * application per rule, ever, unless it expired and re-triggered.
   */
  openOrIssued: ReadonlySet<string>;
  stages?: StageTable;
}

export interface Suggestion {
  /** `${rule}:${applicationId}` — stable, so re-running does not duplicate. */
  key: string;
  rule: NudgeRule;
  applicationIds: string[];
  /** Card copy. */
  kind: string;
  meta: string;
  title: string;
  body: string;
  cta: string;
  /** When this becomes moot without any user action. */
  expiresAt: Date | null;
  /** What the ranking sorts on: sooner is more urgent. */
  urgencyAt: Date;
  depth: number;
  /** Whether the rule is allowed to produce a push at all (Spec §12). */
  pushable: boolean;
  /** Only the deadline rule may ignore the cap and the quiet window. */
  bypassesBudget: boolean;
}

export function suggestionKey(rule: NudgeRule, applicationId: string): string {
  return `${rule}:${applicationId}`;
}

function fmtList(names: string[]): string {
  if (names.length === 1) return names[0]!;
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

/**
 * Produce every suggestion that is currently true, unranked and unbudgeted.
 * `rankAndCap` applies the display budget; the notifier applies the push budget.
 */
export function evaluateNudges(input: NudgeInput): Suggestion[] {
  const stages = input.stages ?? DEFAULT_STAGES;
  const { now } = input;
  const out: Suggestion[] = [];
  const byId = new Map(input.applications.map((a) => [a.id, a]));
  const isOpen = (key: string): boolean => input.openOrIssued.has(key);

  // ── deadline ─────────────────────────────────────────────────────────────
  // The only hard alert in the system, because it is the only one where silence
  // has a cost you cannot undo.
  for (const d of input.deadlines) {
    const app = byId.get(d.application_id);
    if (!app || app.status !== 'live') continue;
    const hours = hoursBetween(now, d.due_at);
    if (hours <= 0) continue;
    if (hours > NOTIFICATIONS.DEADLINE_SUGGESTION_WINDOW_DAYS * 24) continue;
    const key = suggestionKey('deadline', app.id);
    if (isOpen(key)) continue;
    out.push({
      key,
      rule: 'deadline',
      applicationIds: [app.id],
      kind: 'deadline',
      meta: relativeFuture(now, d.due_at),
      title: `${app.company} ${d.kind.replace(/_/g, '-')} due ${weekday(d.due_at)}`,
      body: `Parsed from the ${d.source} email. This is the only alert allowed to interrupt you.`,
      cta: 'Open brief',
      expiresAt: d.due_at,
      urgencyAt: d.due_at,
      depth: stages.depthOf(app.current_stage),
      pushable: true,
      bypassesBudget: true,
    });
  }

  // ── prepare ──────────────────────────────────────────────────────────────
  // No advice generation — just everything you already wrote, in one place, at
  // the right hour.
  for (const iv of input.interviews) {
    const app = byId.get(iv.application_id);
    if (!app || app.status !== 'live') continue;
    const hours = hoursBetween(now, iv.starts_at);
    if (hours <= 0 || hours > NOTIFICATIONS.PREPARE_WINDOW_HOURS) continue;
    const key = suggestionKey('prepare', app.id);
    if (isOpen(key)) continue;
    out.push({
      key,
      rule: 'prepare',
      applicationIds: [app.id],
      kind: 'prepare',
      meta: relativeFuture(now, iv.starts_at),
      title: `${app.company} ${stages.labelOf(iv.stage).toLowerCase()} ${relativeFuture(now, iv.starts_at)}`,
      body: 'The posting, the thread, and everything you have already written about this company, in one place.',
      cta: 'Open the brief',
      expiresAt: iv.starts_at,
      urgencyAt: iv.starts_at,
      depth: stages.depthOf(app.current_stage),
      pushable: true,
      bypassesBudget: false,
    });
  }

  // ── follow_up_due ────────────────────────────────────────────────────────
  // Dwell past p75 of the user's *own* history for that stage. The fallback
  // exists because a new user has no history and would otherwise never be
  // nudged at all.
  for (const app of input.applications) {
    if (app.status !== 'live' || !app.awaiting_them || !app.last_signal_at) continue;
    const dwell = daysBetween(app.last_signal_at, now);
    const p75 = input.p75DwellDays(app.current_stage);
    const threshold = p75 ?? stages.staleAfterDays(app.current_stage) * 0.6;
    if (dwell <= threshold) continue;
    const key = suggestionKey('follow_up_due', app.id);
    if (isOpen(key)) continue;
    const p50 = input.p50DwellDays(app.current_stage);
    const label = stages.labelOf(app.current_stage).toLowerCase();
    out.push({
      key,
      rule: 'follow_up_due',
      applicationIds: [app.id],
      kind: 'follow-up due',
      meta: relativePast(now, app.last_signal_at),
      title: `${app.company} has gone quiet since the ${label}`,
      body:
        p50 === null
          ? `Nothing has come back in ${dwell} days. A short nudge is normal here.`
          : `You last heard from them ${dwell} days ago and your median wait at this stage is ${p50} days. A short nudge is normal here.`,
      cta: 'Draft follow-up',
      expiresAt: new Date(now.getTime() + NOTIFICATIONS.FOLLOW_UP_EXPIRY_DAYS * 86_400_000),
      urgencyAt: new Date(app.last_signal_at.getTime() + threshold * 86_400_000),
      depth: stages.depthOf(app.current_stage),
      pushable: true,
      bypassesBudget: false,
    });
  }

  // ── let_it_go ────────────────────────────────────────────────────────────
  // Batched into one card on purpose: this is the rule that keeps the pipeline
  // honest without making you admit defeat one card at a time. Never pushed.
  const lettable = input.applications.filter((app) => {
    if (app.status !== 'dormant' || !app.went_dormant_at) return false;
    if (daysBetween(app.went_dormant_at, now) < NOTIFICATIONS.LET_IT_GO_AFTER_DORMANT_DAYS) return false;
    if (app.last_user_action_at && app.last_user_action_at > app.went_dormant_at) return false;
    return !isOpen(suggestionKey('let_it_go', app.id));
  });
  if (lettable.length > 0) {
    const ids = lettable.map((a) => a.id);
    const oldest = lettable.reduce((acc, a) =>
      (a.went_dormant_at?.getTime() ?? 0) < (acc.went_dormant_at?.getTime() ?? 0) ? a : acc,
    );
    out.push({
      key: suggestionKey('let_it_go', ids.join('+')),
      rule: 'let_it_go',
      applicationIds: ids,
      kind: 'let it go',
      meta: `${ids.length} application${ids.length === 1 ? '' : 's'}`,
      title: `${fmtList(lettable.map((a) => a.company))} look${lettable.length === 1 ? 's' : ''} finished`,
      body: 'Silent past twice your usual wait. Archiving keeps your ratios honest — they stay in the statistics as ghosted.',
      cta: ids.length === 1 ? 'Archive it' : 'Archive both',
      expiresAt: null,
      urgencyAt: oldest.went_dormant_at ?? now,
      depth: Math.max(...lettable.map((a) => stages.depthOf(a.current_stage))),
      pushable: false,
      bypassesBudget: false,
    });
  }

  return out;
}

/** At most three, ranked by urgency then depth. */
export function rankAndCap(
  suggestions: readonly Suggestion[],
  max = NOTIFICATIONS.MAX_OPEN_SUGGESTIONS,
): Suggestion[] {
  return [...suggestions]
    .sort((a, b) => {
      const r = RULE_URGENCY[a.rule] - RULE_URGENCY[b.rule];
      if (r !== 0) return r;
      const t = a.urgencyAt.getTime() - b.urgencyAt.getTime();
      if (t !== 0) return t;
      const d = b.depth - a.depth;
      if (d !== 0) return d;
      return a.key.localeCompare(b.key);
    })
    .slice(0, max);
}

function weekday(d: Date): string {
  return new Intl.DateTimeFormat('en-GB', { weekday: 'long', timeZone: 'UTC' }).format(d);
}
