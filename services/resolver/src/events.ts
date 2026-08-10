import type { EventType, PendingEvent, Signal } from '@loop/domain';

/**
 * Intent → event. The closed set of fourteen types is the only vocabulary the
 * pipeline accepts, so this is where an extracted observation becomes a claim
 * about the world.
 */

export function eventsForSignal(
  signal: Signal,
  applicationId: string,
): PendingEvent[] {
  const base = {
    user_id: signal.user_id,
    application_id: applicationId,
  };
  const common = {
    occurred_at: signal.occurred_at,
    confidence: signal.confidence,
    evidence_ref: signal.evidence_ref,
    rung: signal.rung,
  };
  const fields = {
    thread_id: signal.thread_id,
    role_title: signal.role,
    location: signal.location,
    work_mode: signal.work_mode,
    channel: signal.channel,
  };

  const one = (type: EventType, payload: Record<string, unknown> = {}, toStage?: string): PendingEvent => ({
    ...base,
    event: { type, ...common, to_stage: toStage ?? null, payload: { ...fields, ...payload } },
  });

  const source = signal.channel
    ? {
        channel: signal.channel,
        posting_url: signal.posting_url,
        ats_vendor: signal.ats_vendor,
      }
    : undefined;

  switch (signal.intent) {
    case 'applied': {
      const ev = one('applied', { posting_url: signal.posting_url }, 'applied');
      return [{ ...ev, source: source ? { ...source, is_first_touch: true } : undefined }];
    }

    case 'acknowledged':
      return [one('acknowledged', { ats_vendor: signal.ats_vendor }, 'acknowledged')];

    case 'schedule_screening':
      return [one('stage_advanced', { note: 'availability requested' }, signal.stage_hint ?? 'recruiter_reachout')];

    case 'interview_invite': {
      if (!signal.invite) {
        return [one('stage_advanced', {}, signal.stage_hint ?? 'technical')];
      }
      return [
        one(
          'interview_scheduled',
          {
            stage: signal.stage_hint ?? 'technical',
            starts_at: signal.invite.starts_at,
            ends_at: signal.invite.ends_at,
            location: signal.invite.location,
            calendar_event_id: signal.invite.uid,
            status: 'confirmed',
          },
          signal.stage_hint ?? 'technical',
        ),
      ];
    }

    case 'interview_cancelled': {
      // The claim is withdrawn, but the reversal is never automatic: a cancelled
      // round can mean "rescheduling" or "it is over", and only the user knows
      // which. The pipeline drops the stage claim; the resolver raises a card.
      if (!signal.invite) return [];
      return [
        one('interview_scheduled', {
          stage: signal.stage_hint ?? 'technical',
          starts_at: signal.invite.starts_at,
          ends_at: signal.invite.ends_at,
          calendar_event_id: signal.invite.uid,
          status: 'cancelled',
        }),
      ];
    }

    case 'take_home': {
      const out: PendingEvent[] = [one('stage_advanced', {}, 'take_home')];
      if (signal.deadline) {
        out.push(
          one('deadline_set', {
            kind: 'take_home',
            due_at: signal.deadline,
            url: signal.posting_url,
            source: signal.ats_vendor ?? 'gmail',
          }),
        );
      }
      return out;
    }

    case 'rejected':
      return [one('rejected', { after_stage: signal.stage_hint })];

    case 'offer':
      return [
        one(
          'offer_received',
          {
            min_minor: signal.comp?.min_minor,
            max_minor: signal.comp?.max_minor ?? null,
            currency: signal.comp?.currency ?? 'EUR',
            decide_by: signal.decide_by ?? null,
          },
          'offer',
        ),
      ];

    case 'negotiation':
      return [
        one(
          'offer_negotiated',
          { min_minor: signal.comp?.min_minor, currency: signal.comp?.currency ?? 'EUR' },
          'negotiating',
        ),
      ];

    // `other` and `unclear` produce nothing. A rung that reached here without a
    // claim has already abstained, and inventing a stage change from silence is
    // exactly the failure this system is built to avoid.
    default:
      return [];
  }
}
