import { describe, expect, it } from 'vitest';
import { activityOf, closureDays, isOpen } from './activity.js';

const now = new Date('2026-08-14T09:00:00Z');
const daysAgo = (n: number): Date => new Date(now.getTime() - n * 86_400_000);

const base = {
  now,
  status: 'live',
  currentStage: 'hr_call',
  currentPhase: 'screening',
  presumedClosed: false,
  lastSignalAt: daysAgo(2),
  nextInterviewAt: null,
  quietThresholdDays: 21,
};

describe('activityOf', () => {
  it('reads a recorded outcome before anything else', () => {
    for (const status of ['rejected', 'withdrawn', 'accepted', 'dormant']) {
      expect(activityOf({ ...base, status, lastSignalAt: daysAgo(1) })).toBe('closed');
    }
  });

  it('keeps an application with an interview in the diary active however quiet it is', () => {
    expect(
      activityOf({
        ...base,
        lastSignalAt: daysAgo(200),
        nextInterviewAt: new Date(now.getTime() + 3 * 86_400_000),
      }),
    ).toBe('active');
  });

  it('trusts the sweep once it has presumed closure', () => {
    expect(activityOf({ ...base, presumedClosed: true })).toBe('closed');
  });

  it('never writes off a stage where the ball is in your court', () => {
    for (const stage of ['take_home', 'offer', 'negotiating']) {
      expect(activityOf({ ...base, currentStage: stage, lastSignalAt: daysAgo(300) })).toBe('active');
    }
  });

  it('closes an acknowledged application nobody has answered in two months', () => {
    const acknowledged = { ...base, currentStage: 'acknowledged', currentPhase: 'sent' };
    expect(activityOf({ ...acknowledged, lastSignalAt: daysAgo(59) })).toBe('stale');
    expect(activityOf({ ...acknowledged, lastSignalAt: daysAgo(61) })).toBe('closed');
  });

  it('gives a process that got somewhere the full ninety days', () => {
    expect(activityOf({ ...base, lastSignalAt: daysAgo(61) })).toBe('stale');
    expect(activityOf({ ...base, lastSignalAt: daysAgo(91) })).toBe('closed');
  });

  it('is stale between its stage threshold and closure, active before it', () => {
    expect(activityOf({ ...base, lastSignalAt: daysAgo(20) })).toBe('active');
    expect(activityOf({ ...base, lastSignalAt: daysAgo(22) })).toBe('stale');
  });

  it('uses the stage default when the user has no cadence of their own', () => {
    expect(activityOf({ ...base, quietThresholdDays: null, lastSignalAt: daysAgo(22) })).toBe('stale');
  });

  it('treats a row that has never had a signal as active', () => {
    expect(activityOf({ ...base, lastSignalAt: null })).toBe('active');
  });

  it('closes earlier before a reply than after one', () => {
    expect(closureDays('sent')).toBe(60);
    expect(closureDays('screening')).toBe(90);
    expect(closureDays('interviewing')).toBe(90);
  });

  it('counts stale as open, because a follow-up is still worth sending', () => {
    expect(isOpen('active')).toBe(true);
    expect(isOpen('stale')).toBe(true);
    expect(isOpen('closed')).toBe(false);
  });
});
