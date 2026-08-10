import { describe, expect, it } from 'vitest';
import { evaluateNudges, rankAndCap, type AppSnapshot, type NudgeInput } from './nudges.js';

const now = new Date('2026-07-30T09:00:00Z');

function app(over: Partial<AppSnapshot> = {}): AppSnapshot {
  return {
    id: 'a1',
    company: 'Nexi',
    role_title: 'Platform Engineer',
    current_stage: 'onsite_loop',
    status: 'live',
    last_signal_at: new Date('2026-07-24T09:00:00Z'),
    awaiting_them: true,
    last_user_action_at: null,
    went_dormant_at: null,
    ...over,
  };
}

function input(over: Partial<NudgeInput> = {}): NudgeInput {
  return {
    now,
    applications: [app()],
    interviews: [],
    deadlines: [],
    p75DwellDays: () => 3,
    p50DwellDays: () => 3,
    openOrIssued: new Set<string>(),
    ...over,
  };
}

describe('follow_up_due', () => {
  it('fires past p75 of the user’s own history', () => {
    const s = evaluateNudges(input());
    expect(s).toHaveLength(1);
    expect(s[0]!.rule).toBe('follow_up_due');
    expect(s[0]!.meta).toBe('6 days quiet');
    expect(s[0]!.cta).toBe('Draft follow-up');
  });

  it('stays silent inside the user’s normal wait', () => {
    expect(evaluateNudges(input({ p75DwellDays: () => 30 }))).toHaveLength(0);
  });

  it('falls back to stale_after_days × 0.6 when there is no history yet', () => {
    // onsite_loop is stale after 14 days → threshold 8.4, and 6 days is inside it.
    expect(evaluateNudges(input({ p75DwellDays: () => null, p50DwellDays: () => null }))).toHaveLength(0);
    const older = app({ last_signal_at: new Date('2026-07-19T09:00:00Z') }); // 11 days
    const s = evaluateNudges(
      input({ applications: [older], p75DwellDays: () => null, p50DwellDays: () => null }),
    );
    expect(s).toHaveLength(1);
    expect(s[0]!.body).not.toContain('median');
  });

  it('does not fire when the ball is in the user’s court', () => {
    expect(evaluateNudges(input({ applications: [app({ awaiting_them: false })] }))).toHaveLength(0);
  });

  it('issues at most one per application per rule', () => {
    const s = evaluateNudges(input({ openOrIssued: new Set(['follow_up_due:a1']) }));
    expect(s).toHaveLength(0);
  });
});

describe('deadline', () => {
  it('fires inside 72 hours and is the only rule that bypasses the budget', () => {
    const s = evaluateNudges(
      input({
        deadlines: [
          { application_id: 'a1', kind: 'take_home', due_at: new Date('2026-08-02T21:59:00Z'), source: 'CodeSubmit' },
        ],
      }),
    );
    const d = s.find((x) => x.rule === 'deadline')!;
    expect(d.bypassesBudget).toBe(true);
    expect(d.title).toBe('Nexi take-home due Sunday');
    expect(d.pushable).toBe(true);
  });

  it('does not fire for a deadline that already passed', () => {
    const s = evaluateNudges(
      input({
        deadlines: [
          { application_id: 'a1', kind: 'take_home', due_at: new Date('2026-07-29T21:59:00Z'), source: 'CodeSubmit' },
        ],
      }),
    );
    expect(s.some((x) => x.rule === 'deadline')).toBe(false);
  });
});

describe('prepare', () => {
  it('fires inside 48 hours of an interview', () => {
    const s = evaluateNudges(
      input({
        interviews: [
          { id: 'i1', application_id: 'a1', stage: 'system_design', starts_at: new Date('2026-07-31T08:00:00Z') },
        ],
      }),
    );
    const p = s.find((x) => x.rule === 'prepare')!;
    expect(p.cta).toBe('Open the brief');
    expect(p.bypassesBudget).toBe(false);
    // No advice generation: the body promises only what the user already wrote.
    expect(p.body).toContain('already');
  });

  it('does not fire three days out', () => {
    const s = evaluateNudges(
      input({
        interviews: [
          { id: 'i1', application_id: 'a1', stage: 'technical', starts_at: new Date('2026-08-03T08:00:00Z') },
        ],
      }),
    );
    expect(s.some((x) => x.rule === 'prepare')).toBe(false);
  });
});

describe('let_it_go', () => {
  const dormant = (id: string, company: string): AppSnapshot =>
    app({
      id,
      company,
      status: 'dormant',
      awaiting_them: false,
      went_dormant_at: new Date('2026-07-10T02:00:00Z'),
    });

  it('batches into one card and is never pushed', () => {
    const s = evaluateNudges(
      input({ applications: [dormant('a1', 'Casavo'), dormant('a2', 'Sportradar')] }),
    );
    const l = s.find((x) => x.rule === 'let_it_go')!;
    expect(l.title).toBe('Casavo and Sportradar look finished');
    expect(l.cta).toBe('Archive both');
    expect(l.applicationIds).toEqual(['a1', 'a2']);
    expect(l.pushable).toBe(false);
  });

  it('backs off once the user has acted', () => {
    const acted = { ...dormant('a1', 'Casavo'), last_user_action_at: new Date('2026-07-20T09:00:00Z') };
    expect(evaluateNudges(input({ applications: [acted] })).some((x) => x.rule === 'let_it_go')).toBe(false);
  });

  it('waits seven days after dormancy', () => {
    const fresh = { ...dormant('a1', 'Casavo'), went_dormant_at: new Date('2026-07-28T02:00:00Z') };
    expect(evaluateNudges(input({ applications: [fresh] })).some((x) => x.rule === 'let_it_go')).toBe(false);
  });
});

describe('the display budget', () => {
  it('shows at most three, ranked by urgency then depth', () => {
    const apps: AppSnapshot[] = [
      app({ id: 'a1', company: 'Nexi', current_stage: 'onsite_loop' }),
      app({ id: 'a2', company: 'Docebo', current_stage: 'hr_call' }),
      app({ id: 'a3', company: 'Everli', current_stage: 'applied' }),
    ];
    const s = evaluateNudges(
      input({
        applications: apps,
        p75DwellDays: () => 1,
        deadlines: [
          { application_id: 'a2', kind: 'take_home', due_at: new Date('2026-08-01T12:00:00Z'), source: 'CodeSubmit' },
        ],
        interviews: [
          { id: 'i1', application_id: 'a3', stage: 'technical', starts_at: new Date('2026-07-31T08:00:00Z') },
        ],
      }),
    );
    const ranked = rankAndCap(s);
    expect(ranked).toHaveLength(3);
    expect(ranked.map((x) => x.rule)).toEqual(['deadline', 'prepare', 'follow_up_due']);
    // Within follow_up_due the deeper stage would come first — here only one survives the cap.
    expect(ranked[2]!.applicationIds[0]).toBe('a1');
  });
});
