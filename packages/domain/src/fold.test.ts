import { describe, expect, it } from 'vitest';
import { fold, foldWithProvenance } from './fold.js';
import type { DomainEvent, EventType } from './types.js';

const at = (iso: string): Date => new Date(iso);

let seq = 0;
function ev(
  type: EventType,
  occurred: string,
  confidence: number,
  extra: Partial<DomainEvent> = {},
): DomainEvent {
  seq += 1;
  return {
    id: seq,
    type,
    occurred_at: at(occurred),
    confidence,
    evidence_ref: extra.evidence_ref ?? `msg-${seq}`,
    rung: extra.rung ?? 1,
    ...extra,
  };
}

/** Deterministic shuffle so a failure is reproducible. */
function shuffle<T>(xs: readonly T[], seedIn: number): T[] {
  const out = [...xs];
  let seed = seedIn;
  for (let i = out.length - 1; i > 0; i--) {
    seed = (seed * 1103515245 + 12345) % 2147483648;
    const j = seed % (i + 1);
    [out[i], out[j]] = [out[j]!, out[i]!];
  }
  return out;
}

describe('fold — the case the spec got wrong', () => {
  it('advances past a high-confidence auto-reply (Architecture §03 end to end)', () => {
    // The exact sequence the Architecture sheet walks: an ATS acknowledgement at
    // 0.99 followed eleven days later by a recruiter reply at 0.94. Under the
    // literal §05 rule ("highest confidence wins") the stage would stay at
    // `acknowledged` forever.
    const events = [
      ev('applied', '2026-07-02T09:00:00Z', 1.0, { rung: 4, evidence_ref: null }),
      ev('acknowledged', '2026-07-02T09:04:00Z', 0.99),
      ev('stage_advanced', '2026-07-13T11:00:00Z', 0.94, { to_stage: 'hr_call' }),
    ];
    const s = fold(events);
    expect(s.current_stage).toBe('hr_call');
    expect(s.current_phase).toBe('screening');
    expect(s.confidence).toBe(0.94);
  });

  it('lets a human correction beat a later, higher-confidence automated event', () => {
    const events = [
      ev('applied', '2026-07-02T09:00:00Z', 1.0),
      ev('stage_advanced', '2026-07-10T09:00:00Z', 0.95, { to_stage: 'technical' }),
      ev('human_corrected', '2026-07-11T09:00:00Z', 1.0, {
        rung: 4,
        evidence_ref: null,
        payload: { field: 'stage', from: 'technical', to: 'hr_call' },
      }),
      ev('stage_advanced', '2026-07-12T09:00:00Z', 0.99, { to_stage: 'technical' }),
    ];
    // The pin is older than the last automated event but still wins: "the agent
    // is never allowed to argue with you twice".
    expect(fold(events).current_stage).toBe('hr_call');
  });
});

describe('fold — determinism', () => {
  const events = [
    ev('applied', '2026-06-12T08:00:00Z', 1.0),
    ev('acknowledged', '2026-06-13T08:00:00Z', 0.99),
    ev('stage_advanced', '2026-07-01T08:00:00Z', 0.95, { to_stage: 'hr_call' }),
    ev('interview_scheduled', '2026-07-15T08:00:00Z', 0.97, { payload: { stage: 'technical' } }),
    ev('stage_advanced', '2026-07-24T08:00:00Z', 0.9, { to_stage: 'onsite_loop' }),
    ev('went_silent', '2026-08-20T02:00:00Z', 0.9, { rung: null, evidence_ref: null }),
  ];

  it('replaying in any arrival order yields the same state', () => {
    const reference = fold(events);
    for (let seed = 1; seed <= 200; seed++) {
      expect(fold(shuffle(events, seed))).toEqual(reference);
    }
  });

  it('does not depend on the serial id — the spec tie-break would have', () => {
    const a = events.map((e, i) => ({ ...e, id: i }));
    const b = events.map((e, i) => ({ ...e, id: events.length - i }));
    expect(fold(a)).toEqual(fold(b));
  });
});

describe('fold — status', () => {
  it('a new signal brings a dormant application back to live', () => {
    const events = [
      ev('applied', '2026-05-01T08:00:00Z', 1.0),
      ev('went_silent', '2026-06-20T02:00:00Z', 0.9, { rung: null, evidence_ref: null }),
      ev('stage_advanced', '2026-06-25T08:00:00Z', 0.93, { to_stage: 'hr_call' }),
    ];
    expect(fold(events).status).toBe('live');
  });

  it('went_silent never touches the stage', () => {
    const events = [
      ev('applied', '2026-05-01T08:00:00Z', 1.0),
      ev('stage_advanced', '2026-05-10T08:00:00Z', 0.93, { to_stage: 'technical' }),
      ev('went_silent', '2026-06-20T02:00:00Z', 0.9, { rung: null, evidence_ref: null }),
    ];
    const s = fold(events);
    expect(s.status).toBe('dormant');
    expect(s.current_stage).toBe('technical');
    // The funnel keeps it in its denominator precisely because the stage stands.
    expect(s.current_phase).toBe('interviewing');
  });

  it('a rejection is not undone by a later automated signal', () => {
    const events = [
      ev('applied', '2026-05-01T08:00:00Z', 1.0),
      ev('rejected', '2026-06-01T08:00:00Z', 0.97, { payload: { after_stage: 'technical' } }),
      ev('stage_advanced', '2026-06-05T08:00:00Z', 0.99, { to_stage: 'final' }),
    ];
    const s = fold(events);
    expect(s.status).toBe('rejected');
    // Frozen: "how far did it get" must not be rewritten by stray later mail.
    expect(s.current_stage).not.toBe('final');
  });

  it('a human correction reopens a rejection and unfreezes the stage', () => {
    const events = [
      ev('applied', '2026-05-01T08:00:00Z', 1.0),
      ev('rejected', '2026-06-01T08:00:00Z', 0.97),
      ev('human_corrected', '2026-06-02T08:00:00Z', 1.0, {
        rung: 4,
        evidence_ref: null,
        payload: { field: 'status', from: 'rejected', to: 'live' },
      }),
      ev('stage_advanced', '2026-06-05T08:00:00Z', 0.95, { to_stage: 'final' }),
    ];
    const s = fold(events);
    expect(s.status).toBe('live');
    expect(s.current_stage).toBe('final');
  });
});

describe('fold — the confidence floor', () => {
  it('ignores events below the review threshold but keeps them visible', () => {
    const weak = ev('stage_advanced', '2026-07-20T08:00:00Z', 0.54, { to_stage: 'offer', rung: 3 });
    const events = [ev('applied', '2026-07-01T08:00:00Z', 1.0), weak];
    const { state, provenance } = foldWithProvenance(events);
    expect(state.current_stage).toBe('applied');
    expect(provenance.ignoredBelowFloor).toHaveLength(1);
    expect(provenance.ignoredBelowFloor[0]!.confidence).toBe(0.54);
  });
});

describe('fold — dates and payload fields', () => {
  it('applied_at is the earliest applied event, not the latest', () => {
    const events = [
      ev('applied', '2026-07-20T08:00:00Z', 0.98, { payload: { channel: 'linkedin' } }),
      ev('applied', '2026-07-12T08:00:00Z', 0.98, { payload: { channel: 'career_page' } }),
    ];
    expect(fold(events).applied_at?.toISOString()).toBe('2026-07-12T08:00:00.000Z');
  });

  it('last_signal_at ignores notes and corrections', () => {
    const events = [
      ev('applied', '2026-07-01T08:00:00Z', 1.0),
      ev('acknowledged', '2026-07-01T08:05:00Z', 0.99),
      ev('note_added', '2026-07-30T08:00:00Z', 1.0, { rung: null, evidence_ref: null, payload: { text: 'ask about the team' } }),
    ];
    expect(fold(events).last_signal_at?.toISOString()).toBe('2026-07-01T08:05:00.000Z');
  });

  it('carries descriptive fields from payloads so the row can be rebuilt', () => {
    const events = [
      ev('applied', '2026-07-01T08:00:00Z', 0.98, {
        payload: {
          role_title: 'Backend Engineer',
          seniority: 'senior',
          location: 'Milan',
          work_mode: 'hybrid',
          company_id: 'c-1',
          channel: 'career_page',
        },
      }),
    ];
    const s = fold(events);
    expect(s.role_title).toBe('Backend Engineer');
    expect(s.work_mode).toBe('hybrid');
    expect(s.company_id).toBe('c-1');
    expect(s.channel).toBe('career_page');
  });

  it('a correction pins a descriptive field', () => {
    const events = [
      ev('applied', '2026-07-01T08:00:00Z', 0.98, { payload: { role_title: 'Backend Eng' } }),
      ev('human_corrected', '2026-07-02T08:00:00Z', 1.0, {
        rung: 4,
        evidence_ref: null,
        payload: { field: 'role_title', from: 'Backend Eng', to: 'Platform Engineer' },
      }),
      ev('stage_advanced', '2026-07-09T08:00:00Z', 0.99, {
        to_stage: 'hr_call',
        payload: { role_title: 'Backend Eng' },
      }),
    ];
    expect(fold(events).role_title).toBe('Platform Engineer');
  });
});

describe('fold — edges', () => {
  it('an empty log folds to a usable neutral state', () => {
    const s = fold([]);
    expect(s.status).toBe('live');
    expect(s.applied_at).toBeNull();
    expect(s.confidence).toBe(0);
  });

  it('an offer sets stage and phase together', () => {
    const events = [
      ev('applied', '2026-06-02T08:00:00Z', 1.0),
      ev('offer_received', '2026-07-28T08:00:00Z', 0.9, {
        payload: { min_minor: 6_800_000, currency: 'EUR', decide_by: '2026-08-08' },
      }),
    ];
    const s = fold(events);
    expect(s.current_stage).toBe('offer');
    expect(s.current_phase).toBe('decided');
    expect(s.status).toBe('live');
  });
});
