import { describe, expect, it } from 'vitest';
import {
  atLocalTime,
  isQuietHour,
  localParts,
  nextDeliverableAt,
  parseQuietHours,
  zonedToUtc,
} from './time.js';

const ROME = 'Europe/Rome';
const q = parseQuietHours('21:00-08:00');

describe('zoned time', () => {
  it('resolves a wall clock in summer time', () => {
    // 30 July 2026, 03:00 in Rome is 01:00 UTC (CEST, +2).
    expect(zonedToUtc(ROME, 2026, 7, 30, 3, 0).toISOString()).toBe('2026-07-30T01:00:00.000Z');
  });

  it('resolves a wall clock in winter time', () => {
    // 30 January 2026, 03:00 in Rome is 02:00 UTC (CET, +1).
    expect(zonedToUtc(ROME, 2026, 1, 30, 3, 0).toISOString()).toBe('2026-01-30T02:00:00.000Z');
  });

  it('survives the autumn fold without moving the dormancy job off 03:00', () => {
    // Clocks go back on 25 October 2026 at 03:00 local.
    const d = zonedToUtc(ROME, 2026, 10, 25, 3, 0);
    expect(localParts(d, ROME).hour).toBe(3);
  });

  it('resolves the spring gap to the instant just after the jump', () => {
    // 29 March 2026: 02:00–03:00 local does not exist.
    const d = zonedToUtc(ROME, 2026, 3, 29, 2, 30);
    expect(localParts(d, ROME).hour).toBe(3);
  });
});

describe('quiet hours', () => {
  it('covers the window that wraps past midnight', () => {
    expect(isQuietHour(zonedToUtc(ROME, 2026, 7, 30, 22, 0), ROME, q)).toBe(true);
    expect(isQuietHour(zonedToUtc(ROME, 2026, 7, 31, 3, 0), ROME, q)).toBe(true);
    expect(isQuietHour(zonedToUtc(ROME, 2026, 7, 31, 7, 59), ROME, q)).toBe(true);
    expect(isQuietHour(zonedToUtc(ROME, 2026, 7, 31, 8, 0), ROME, q)).toBe(false);
    expect(isQuietHour(zonedToUtc(ROME, 2026, 7, 30, 18, 0), ROME, q)).toBe(false);
    expect(isQuietHour(zonedToUtc(ROME, 2026, 7, 30, 20, 59), ROME, q)).toBe(false);
  });

  it('defers a late-night notification to 08:00 the next morning', () => {
    const late = zonedToUtc(ROME, 2026, 7, 30, 23, 30);
    const when = nextDeliverableAt(late, ROME, q);
    const p = localParts(when, ROME);
    expect([p.day, p.hour, p.minute]).toEqual([31, 8, 0]);
  });

  it('defers a 03:00 notification to 08:00 the same morning', () => {
    const early = zonedToUtc(ROME, 2026, 7, 31, 3, 0);
    const p = localParts(nextDeliverableAt(early, ROME, q), ROME);
    expect([p.day, p.hour]).toEqual([31, 8]);
  });

  it('passes a notification straight through outside the window', () => {
    const noon = zonedToUtc(ROME, 2026, 7, 30, 12, 0);
    expect(nextDeliverableAt(noon, ROME, q)).toBe(noon);
  });
});

describe('the daily slot', () => {
  it('lands on 18:00 local whatever the server timezone is', () => {
    const now = new Date('2026-07-30T04:12:00Z');
    const slot = atLocalTime(now, ROME, '18:00');
    expect(localParts(slot, ROME).hour).toBe(18);
    // …and in a zone on the other side of the world.
    const tokyo = atLocalTime(now, 'Asia/Tokyo', '18:00');
    expect(localParts(tokyo, 'Asia/Tokyo').hour).toBe(18);
  });
});
