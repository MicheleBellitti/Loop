import { describe, expect, it } from 'vitest';
import { channelGate, dwellMetric, formatPercent, ratio, seasonalGate } from './metrics.js';

describe('ratio', () => {
  it('always carries a note naming numerator, denominator and exclusions', () => {
    const m = ratio({ numerator: 11, denominator: 68, excluded: 5, closed: 30, exclusionReason: 'too recent to count' });
    expect(m.gate_met).toBe(true);
    expect(m.note).toBe('11 of 68 · 5 too recent to count');
    expect(formatPercent(m.value)).toBe('16.2%');
  });

  it('withholds the number below the gate and names the threshold instead', () => {
    const m = ratio({ numerator: 2, denominator: 9, closed: 4 });
    expect(m.value).toBeNull();
    expect(m.gate_met).toBe(false);
    expect(m.note).toBe('4 closed · unlocks at 8 closed applications');
  });

  it('flags a small sample between the two gates', () => {
    const m = ratio({ numerator: 3, denominator: 11, closed: 11 });
    expect(m.small_sample).toBe(true);
    expect(m.note).toContain('small sample');
  });

  it('does not divide by zero', () => {
    expect(ratio({ numerator: 0, denominator: 0, closed: 10 }).value).toBeNull();
  });
});

describe('other gates', () => {
  it('holds time-in-stage until five transitions exist', () => {
    expect(dwellMetric(4.2, 4).value).toBeNull();
    expect(dwellMetric(4.2, 4).note).toBe('4 of 5 stage changes needed');
    expect(dwellMetric(4.2, 5).value).toBe(4.2);
  });

  it('holds a channel row under three applications', () => {
    expect(channelGate(2).gate_met).toBe(false);
    expect(channelGate(3).gate_met).toBe(true);
  });

  it('explains why seasonal shape is hidden rather than noisy', () => {
    expect(seasonalGate(0).note).toContain('2 more quarters');
    expect(seasonalGate(1).note).toContain('1 more quarter');
    expect(seasonalGate(2).gate_met).toBe(true);
  });
});

describe('formatting', () => {
  it('renders one decimal and drops a trailing zero', () => {
    expect(formatPercent(0.27)).toBe('27%');
    expect(formatPercent(0.162)).toBe('16.2%');
    expect(formatPercent(null)).toBe('—');
  });
});
