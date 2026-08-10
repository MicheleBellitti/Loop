import { describe, expect, it } from 'vitest';
import type { RawMessage } from '@loop/domain';
import { classify, type ClassifierContext } from './classify.js';

const ctx: ClassifierContext = {
  atsDomains: ['greenhouse-mail.io', 'hire.lever.co', 'myworkday.com'],
  companyDomains: new Set(['nexi.it']),
  knownThreads: new Set(['thread-1']),
  knownNewsletters: new Set(['newsletter.example.com']),
};

function msg(over: Partial<RawMessage> & { headers?: Partial<RawMessage['headers']> } = {}): RawMessage {
  const { headers, ...rest } = over;
  return {
    user_id: 'u1',
    mailbox_id: 'm1',
    provider_message_id: 'p1',
    thread_id: null,
    received_at: '2026-07-30T09:00:00Z',
    headers: {
      message_id: '<a@b>',
      from: 'someone@example.com',
      to: ['you@gmail.com'],
      subject: 'Hello',
      date: '2026-07-30T09:00:00Z',
      ...headers,
    },
    text: 'Nothing much here.',
    body_sha256: 'x',
    invite: null,
    ...rest,
  };
}

describe('the LinkedIn/Indeed trap', () => {
  // "This is the single most common false-negative in the whole system."
  it('keeps a LinkedIn confirmation that arrives bulk-flagged', () => {
    const r = classify(
      msg({
        headers: {
          from: 'jobs-noreply@linkedin.com',
          subject: 'Your application was sent to Nexi',
          list_unsubscribe: '<https://linkedin.com/unsub>',
          list_id: 'jobs.linkedin.com',
          precedence: 'bulk',
        },
        text: 'Your application for Platform Engineer was sent to Nexi.',
      }),
      ctx,
    );
    expect(r.outcome).not.toBe('drop');
    expect(r.reasons.join(' ')).toContain('waived');
  });

  it('keeps an Indeed confirmation the same way', () => {
    const r = classify(
      msg({
        headers: {
          from: 'noreply@indeedemail.com',
          subject: 'Indeed Application: Backend Engineer - Everli',
          precedence: 'bulk',
          list_id: 'indeed',
        },
        text: 'You applied to Backend Engineer at Everli.',
      }),
      ctx,
    );
    expect(r.outcome).not.toBe('drop');
  });
});

describe('what passes', () => {
  it('passes an ATS auto-reply outright', () => {
    const r = classify(
      msg({
        headers: { from: 'no-reply@eu.greenhouse-mail.io', subject: 'Thank you for applying to Zalando' },
        text: 'We have received your application.',
      }),
      ctx,
    );
    expect(r.score).toBeGreaterThanOrEqual(3);
    expect(r.outcome).toBe('pass');
  });

  it('passes direct mail from a company already in the pipeline', () => {
    const r = classify(
      msg({
        headers: { from: 'Marta <talent@nexi.it>', subject: 'Colloquio tecnico' },
        text: 'Sei disponibile giovedì per il colloquio?',
      }),
      ctx,
    );
    expect(r.outcome).toBe('pass');
  });

  it('passes a reply on a thread it already owns', () => {
    const r = classify(
      msg({
        thread_id: 'thread-1',
        headers: { from: 'someone@unknown.com', subject: 'Re: interview' },
      }),
      ctx,
    );
    expect(r.outcome).not.toBe('drop');
  });

  it('passes anything carrying a calendar invite', () => {
    const r = classify(
      msg({
        headers: { from: 'talent@somecompany.com', subject: 'Invitation' },
        invite: {
          uid: 'ics-1',
          summary: 'Technical interview',
          starts_at: '2026-07-31T08:00:00Z',
          ends_at: null,
          location: null,
          organiser: 'talent@somecompany.com',
          attendees: [],
          status: 'confirmed',
          method: 'REQUEST',
        },
      }),
      ctx,
    );
    expect(r.outcome).not.toBe('drop');
  });
});

describe('what drops', () => {
  it('drops a newsletter', () => {
    const r = classify(
      msg({
        headers: {
          from: 'news@newsletter.example.com',
          subject: 'This week in tech',
          list_id: 'weekly',
          precedence: 'bulk',
        },
      }),
      ctx,
    );
    expect(r.outcome).toBe('drop');
  });

  it('drops a GitHub notification', () => {
    const r = classify(
      msg({
        headers: { from: 'notifications@github.com', subject: '[repo] PR merged', list_id: 'repo' },
      }),
      ctx,
    );
    expect(r.outcome).toBe('drop');
  });

  it('sends a borderline message down the cheap rungs only, never the model', () => {
    const r = classify(
      msg({
        headers: { from: 'someone@unknown-company.com', subject: 'About the role' },
        text: 'Following up about the role we discussed.',
      }),
      ctx,
    );
    expect(r.outcome).toBe('cheap_only');
  });
});

describe('recall bias', () => {
  it('does not punish a no-reply sender that mentions an application', () => {
    const r = classify(
      msg({
        headers: { from: 'no-reply@somecompany.com', subject: 'Your application' },
        text: 'We received your application for the backend position.',
      }),
      ctx,
    );
    // The -3 no-reply penalty only applies without a vocabulary hit; here the
    // keyword saves it, which is the whole point of biasing towards recall.
    expect(r.outcome).not.toBe('drop');
  });
});
