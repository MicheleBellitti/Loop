import type { Phase, StageDef, AppStatus } from './types.js';

/**
 * The default stage set, seeded per user and editable afterwards.
 *
 * `draft` from Engineering Spec §10 is absent: no event in the catalogue can
 * produce it (quick add emits `applied`), so it was unreachable, and it was
 * excluded from every metric anyway. Depth 0 is left free so a future `saved`
 * event can reintroduce it without renumbering anything. See decisions.md A7.
 *
 * Depths 3 and 11 are deliberately unused — the spec leaves them free so a user
 * can insert a stage of their own between two defaults.
 */
export const DEFAULT_STAGE_DEFS: readonly StageDef[] = [
  { key: 'applied', label: 'Applied', phase: 'sent', depth: 1, stale_after_days: 21 },
  { key: 'acknowledged', label: 'Acknowledged', phase: 'sent', depth: 2, stale_after_days: 21 },
  { key: 'recruiter_reachout', label: 'Recruiter reach-out', phase: 'screening', depth: 4, stale_after_days: 10 },
  { key: 'hr_call', label: 'HR call', phase: 'screening', depth: 5, stale_after_days: 10 },
  { key: 'take_home', label: 'Take-home', phase: 'screening', depth: 6, stale_after_days: 14 },
  { key: 'technical', label: 'Technical', phase: 'interviewing', depth: 7, stale_after_days: 12 },
  { key: 'system_design', label: 'System design', phase: 'interviewing', depth: 8, stale_after_days: 12 },
  { key: 'onsite_loop', label: 'Onsite loop', phase: 'interviewing', depth: 9, stale_after_days: 14 },
  { key: 'final', label: 'Final', phase: 'interviewing', depth: 10, stale_after_days: 10 },
  { key: 'offer', label: 'Offer', phase: 'decided', depth: 12, stale_after_days: 7 },
  { key: 'negotiating', label: 'Negotiating', phase: 'decided', depth: 13, stale_after_days: 7 },
];

/** Groups render in this fixed order; empty groups are omitted entirely. */
export const PHASE_ORDER: readonly Phase[] = ['interviewing', 'screening', 'sent', 'decided'];

export const PHASE_LABELS: Record<Phase, string> = {
  sent: 'Sent',
  screening: 'Screening',
  interviewing: 'Interviewing',
  decided: 'Decided',
};

/**
 * A user's stage_defs may be edited, so every lookup goes through a table the
 * caller supplies. Renaming a stage is a label change, never a data migration —
 * which is why existing events keep their original `to_stage` string and an
 * unknown key degrades gracefully instead of throwing.
 */
export class StageTable {
  private readonly byKey: Map<string, StageDef>;

  constructor(defs: readonly StageDef[] = DEFAULT_STAGE_DEFS) {
    this.byKey = new Map(defs.map((d) => [d.key, d]));
  }

  get(key: string): StageDef | undefined {
    return this.byKey.get(key);
  }

  all(): StageDef[] {
    return [...this.byKey.values()].sort((a, b) => a.depth - b.depth);
  }

  /** Unknown stages land in `sent`: visible and uncounted rather than lost. */
  phaseOf(key: string): Phase {
    return this.byKey.get(key)?.phase ?? 'sent';
  }

  depthOf(key: string): number {
    return this.byKey.get(key)?.depth ?? 0;
  }

  labelOf(key: string): string {
    return this.byKey.get(key)?.label ?? key;
  }

  staleAfterDays(key: string): number {
    return this.byKey.get(key)?.stale_after_days ?? 21;
  }

  /**
   * Depth is for display and for "how far did it get" only. It MUST NOT be used
   * to reject a transition — skipping forward is normal (referrals start deep)
   * and moving back is legitimate (a round was added). This exists so the
   * headline can count *forward* movement, not so anything can be blocked.
   */
  isForward(from: string | null | undefined, to: string): boolean {
    if (!from) return true;
    return this.depthOf(to) > this.depthOf(from);
  }
}

export const DEFAULT_STAGES = new StageTable();

/**
 * What the interface shows in the stage column.
 *
 * The prototypes draw `Rejected` and `Dormant` where a stage goes, while the
 * schema calls them statuses. Status wins, and the mapping happens here, on the
 * server, because the client is not allowed to derive a stage. decisions.md A6.
 */
export function displayStage(
  status: AppStatus,
  stage: string,
  stages: StageTable = DEFAULT_STAGES,
): string {
  switch (status) {
    case 'rejected':
      return 'Rejected';
    case 'withdrawn':
      return 'Withdrawn';
    case 'accepted':
      return 'Accepted';
    case 'dormant':
      return 'Dormant';
    default:
      return stages.labelOf(stage);
  }
}

/** Closed applications render dimmed, never hidden. */
export function isClosed(status: AppStatus): boolean {
  return status !== 'live';
}
