import { describe, expect, it } from 'vitest';
import { computeFlag, daysQuiet, quietLabel } from './flags.js';

const now = new Date('2026-07-30T09:00:00Z');
const tz = 'Europe/Rome';

describe('daysQuiet', () => {
  it('floors to whole days and never goes negative', () => {
    expect(daysQuiet(now, new Date('2026-07-24T10:00:00Z'))).toBe(5);
    expect(daysQuiet(now, new Date('2026-07-31T10:00:00Z'))).toBe(0);
    expect(daysQuiet(now, null)).toBeNull();
  });

  it('reads as English in the row meta', () => {
    expect(quietLabel(1)).toBe('quiet 1 day');
    expect(quietLabel(6)).toBe('quiet 6 days');
    expect(quietLabel(null)).toBe('');
  });
});

describe('flag precedence', () => {
  it('a deadline inside 72h outranks everything else', () => {
    const f = computeFlag({
      now,
      tz,
      status: 'live',
      deadlineAt: new Date('2026-08-02T21:59:00Z'),
      decideBy: new Date('2026-08-08T00:00:00Z'),
      lastSignalAt: new Date('2026-06-01T00:00:00Z'),
      quietThresholdDays: 10,
    });
    expect(f.kind).toBe('deadline');
    expect(f.text).toMatch(/^Due Sunday /);
  });

  it('ignores a deadline further out than 72h', () => {
    const f = computeFlag({
      now,
      tz,
      status: 'live',
      deadlineAt: new Date('2026-08-20T21:59:00Z'),
      decideBy: new Date('2026-08-08T00:00:00Z'),
    });
    expect(f.kind).toBe('decide');
    expect(f.text).toBe('decide by 8 Aug');
  });

  it('falls through to quiet when nothing is owed', () => {
    const f = computeFlag({
      now,
      tz,
      status: 'live',
      lastSignalAt: new Date('2026-07-14T09:00:00Z'),
      quietThresholdDays: 10,
    });
    expect(f).toEqual({ kind: 'quiet', text: 'quiet · past your p90' });
  });

  it('flags a dormant application even without a threshold', () => {
    expect(computeFlag({ now, tz, status: 'dormant' }).kind).toBe('quiet');
  });

  it('a closed application carries no flag — it has nothing left to be late for', () => {
    for (const status of ['rejected', 'withdrawn', 'accepted'] as const) {
      const f = computeFlag({
        now,
        tz,
        status,
        deadlineAt: new Date('2026-07-31T09:00:00Z'),
        lastSignalAt: new Date('2026-01-01T09:00:00Z'),
        quietThresholdDays: 1,
      });
      expect(f).toEqual({ kind: 'none', text: '' });
    }
  });

  it('is silent when everything is on time', () => {
    const f = computeFlag({
      now,
      tz,
      status: 'live',
      lastSignalAt: new Date('2026-07-29T09:00:00Z'),
      quietThresholdDays: 10,
    });
    expect(f.kind).toBe('none');
  });
});
