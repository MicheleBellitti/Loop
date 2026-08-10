import { createConsumer, QUEUES } from '@loop/queue';
import { M, startService } from '@loop/runtime';
import {
  appendEvent,
  applyEventSideEffects,
  projectApplication,
  toDomainEvent,
  type EventRow,
} from '@loop/db';
import type { PendingEvent } from '@loop/domain';

/**
 * The pipeline service: the only writer of application state.
 *
 * Concurrency 1, and the database backs that up with grants — no other role
 * holds INSERT on `application_events`. Everything here is idempotent: the
 * unique index on (application_id, type, occurred_at, evidence_ref) means the
 * same queue message delivered twice produces one row and one notification.
 *
 * Projection refresh is debounced by five seconds because a backfill appends
 * thousands of events in a burst and refreshing per event would spend the whole
 * afternoon rebuilding a view nobody is reading yet.
 */

const REFRESH_DEBOUNCE_MS = 5_000;

await startService({ name: 'pipeline', healthPort: 9104 }, async (ctx) => {
  let refreshTimer: NodeJS.Timeout | null = null;
  let refreshPending = false;

  const scheduleRefresh = (): void => {
    refreshPending = true;
    if (refreshTimer) return;
    refreshTimer = setTimeout(() => {
      refreshTimer = null;
      if (!refreshPending) return;
      refreshPending = false;
      void ctx.pool
        .query('select refresh_projections()')
        .catch((err: Error) => ctx.log.error({ msg: 'projection refresh failed', error: err.message }));
    }, REFRESH_DEBOUNCE_MS);
  };

  const consumer = createConsumer(ctx.pool, {
    queue: QUEUES.event,
    batch: 20,
    concurrency: 1,
    log: (line) => ctx.log.debug(line),
  });

  void consumer.start<PendingEvent>(async (pending, _env, sql) => {
    const { user_id: userId, application_id: applicationId, event } = pending;
    await sql.query('select set_config($1,$2,true)', ['loop.user_id', userId]);

    const eventId = await appendEvent(sql, {
      userId,
      applicationId,
      type: event.type,
      occurredAt: new Date(event.occurred_at),
      confidence: event.confidence,
      fromStage: event.from_stage ?? null,
      toStage: event.to_stage ?? null,
      payload: event.payload,
      evidenceRef: event.evidence_ref ?? null,
      rung: event.rung ?? null,
    });

    if (!eventId) {
      // Already present. Idempotency is not an error, and it must not produce a
      // second buzz on the user's phone.
      ctx.log.debug({ application_id: applicationId, event_type: event.type, outcome: 'duplicate' });
      return;
    }

    if (pending.source) {
      // Exactly one first touch per application, enforced by a partial unique
      // index — every channel statistic depends on it.
      await sql.query(
        `insert into sources (user_id, application_id, channel, posting_url, ats_vendor, is_first_touch)
         values ($1,$2,$3,$4,$5, $6 and not exists (
           select 1 from sources where application_id = $2 and is_first_touch
         ))`,
        [
          userId,
          applicationId,
          pending.source.channel,
          pending.source.posting_url ?? null,
          pending.source.ats_vendor ?? null,
          pending.source.is_first_touch ?? false,
        ],
      );
    }

    const rows = await sql.query<EventRow>(`select * from application_events where id = $1`, [eventId]);
    const domainEvent = toDomainEvent(rows.rows[0]!);
    await applyEventSideEffects(sql, userId, applicationId, eventId, domainEvent);
    await projectApplication(sql, userId, applicationId);

    M.eventsAppended.inc({ type: event.type });
    scheduleRefresh();

    if (!pending.silent) {
      // The gateway holds the SSE connections; LISTEN/NOTIFY is how a message
      // that landed in this container reaches a browser attached to that one.
      await sql.query('select pg_notify($1, $2)', [
        'loop_events',
        JSON.stringify({ type: 'application.changed', user_id: userId, application_id: applicationId }),
      ]);
    }

    ctx.log.info({
      application_id: applicationId,
      event_type: event.type,
      confidence: event.confidence,
      rung: event.rung ?? undefined,
      outcome: 'appended',
    });
  });

  return async () => {
    await consumer.stop();
    if (refreshTimer) clearTimeout(refreshTimer);
  };
});
