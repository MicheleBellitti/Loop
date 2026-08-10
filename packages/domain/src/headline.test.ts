import { describe, expect, it } from 'vitest';
import { buildHeadline, dateEyebrow, numberWord } from './headline.js';
import type { DomainEvent, EventType } from './types.js';

const now = new Date('2026-07-30T09:00:00Z');

function ev(type: EventType, iso: string, extra: Partial<DomainEvent> = {}): DomainEvent {
  return { type, occurred_at: new Date(iso), confidence: 0.95, ...extra };
}

const base = {
  applicationIdOf: (e: DomainEvent) => String(e.evidence_ref ?? 'a'),
  now,
  liveCount: 14,
  openSuggestionCount: 3,
};

describe('the Today headline', () => {
  it('counts applications, not events', () => {
    const events = [
      ev('stage_advanced', '2026-07-28T09:00:00Z', { evidence_ref: 'a', to_stage: 'technical', from_stage: 'hr_call' }),
      ev('stage_advanced', '2026-07-29T09:00:00Z', { evidence_ref: 'a', to_stage: 'final', from_stage: 'technical' }),
      ev('offer_received', '2026-07-27T09:00:00Z', { evidence_ref: 'b' }),
      ev('interview_scheduled', '2026-07-29T09:00:00Z', { evidence_ref: 'c' }),
    ];
    const h = buildHeadline({ ...base, events });
    expect(h.movedCount).toBe(3);
    expect(h.lines).toEqual(['Three moved', 'forward', 'this week']);
  });

  it('does not count a backwards stage change as progress', () => {
    const events = [
      ev('stage_advanced', '2026-07-29T09:00:00Z', {
        evidence_ref: 'a',
        from_stage: 'final',
        to_stage: 'technical',
      }),
    ];
    expect(buildHeadline({ ...base, events }).kind).not.toBe('moved');
  });

  it('ignores events outside the week', () => {
    const events = [ev('offer_received', '2026-07-01T09:00:00Z', { evidence_ref: 'a' })];
    expect(buildHeadline({ ...base, events }).movedCount).toBe(0);
  });

  it('falls back to a statement of fact, never to cheer', () => {
    const h = buildHeadline({ ...base, events: [], liveCount: 9, openSuggestionCount: 2 });
    expect(h.lines).toEqual(['Nine applications', 'waiting']);
    expect(h.kind).toBe('waiting');
  });

  it('says the day is clear when nothing needs the user (E2)', () => {
    const h = buildHeadline({ ...base, events: [], liveCount: 12, openSuggestionCount: 0 });
    expect(h.lines).toEqual(['You are', 'clear today']);
  });

  it('says nothing is tracked yet on day one (E1)', () => {
    const h = buildHeadline({ ...base, events: [], liveCount: 0, openSuggestionCount: 0 });
    expect(h.lines).toEqual(['Nothing', 'to track yet']);
  });

  it('never exceeds three lines', () => {
    for (const liveCount of [0, 1, 9, 40]) {
      for (const openSuggestionCount of [0, 3]) {
        const h = buildHeadline({ ...base, events: [], liveCount, openSuggestionCount });
        expect(h.lines.length).toBeLessThanOrEqual(3);
      }
    }
  });
});

describe('numberWord', () => {
  it('spells small numbers and falls back to numerals', () => {
    expect(numberWord(3)).toBe('Three');
    expect(numberWord(12)).toBe('Twelve');
    expect(numberWord(13)).toBe('13');
  });
});

describe('dateEyebrow', () => {
  it('renders the design copy exactly', () => {
    expect(dateEyebrow(new Date('2026-07-30T09:00:00Z'), 'Europe/Rome')).toBe('Thursday 30 July');
  });
});
