import { createConsumer, publish, QUEUES } from '@loop/queue';
import { M, startService } from '@loop/runtime';
import { atsDomains, rules } from '@loop/rules';
import { RESOLVER, type PendingEvent, type Signal } from '@loop/domain';
import { createEmbedder } from './embed.js';
import { findDuplicate, resolve } from './resolve.js';
import { eventsForSignal } from './events.js';

/**
 * The resolver service.
 *
 * Concurrency 1 — ordering matters, and two signals arriving together must not
 * both decide "there is no application at this company yet" and create one
 * each. That constraint is the reason this is its own container.
 */

await startService({ name: 'resolver', healthPort: 9103 }, async (ctx) => {
  const registry = await rules();
  const domains = atsDomains(registry);
  const embedder = createEmbedder();
  ctx.log.info({ msg: 'embedder ready', reason: embedder.name });

  const consumer = createConsumer(ctx.pool, {
    queue: QUEUES.signal,
    batch: 5,
    concurrency: 1,
    log: (line) => ctx.log.debug(line),
  });

  void consumer.start<Signal & { application_hint?: string | null }>(async (signal, _env, sql) => {
    await sql.query('select set_config($1,$2,true)', ['loop.user_id', signal.user_id]);
    const deps = { sql, embedder, atsDomains: domains, now: new Date() };

    const decision = await resolve(deps, signal);
    M.resolverDecisions.inc({ decision: decision.kind });

    if (decision.kind === 'ambiguous') {
      // "Two candidate applications within 0.05 of each other. Lands in an
      // inbox-shaped review queue: one tap to confirm, and the answer is
      // written back as a new template rule or a resolver alias."
      const rows = await sql.query<{ id: string; role_title: string; current_stage: string; applied_at: Date | null }>(
        `select id, role_title, current_stage, applied_at from applications where id = any($1)`,
        [decision.candidates.map((c) => c.id)],
      );
      const candidates = rows.rows.map((r) => ({
        application_id: r.id,
        role_title: r.role_title,
        stage: r.current_stage,
        applied_at: r.applied_at,
        cosine: decision.candidates.find((c) => c.id === r.id)?.cosine ?? 0,
      }));

      await sql.query(
        `insert into review_items (user_id, kind, evidence_ref, excerpt, candidates)
         values ($1, 'ambiguous_match', $2, $3, $4)
         on conflict do nothing`,
        [signal.user_id, signal.evidence_ref, signal.excerpt, JSON.stringify(candidates)],
      );
      await sql.query(
        `update seen_messages set outcome = 'review', processed_at = now()
          where mailbox_id = $1 and provider_message_id = $2`,
        [signal.mailbox_id, signal.provider_message_id],
      );
      ctx.log.info({
        provider_message_id: signal.provider_message_id,
        outcome: 'review',
        decision: 'ambiguous',
        candidates: candidates.length,
      });
      return;
    }

    const applicationId = decision.applicationId;

    // ── cross-channel dedup ────────────────────────────────────────────────
    if (decision.kind === 'created' || decision.kind === 'attached') {
      const dup = await findDuplicate(deps, signal.user_id, applicationId);
      if (dup) {
        await sql.query(`update applications set merged_into_id = $1 where id = $2`, [dup.keep, dup.merge]);
        await sql.query(`update sources set application_id = $1 where application_id = $2`, [dup.keep, dup.merge]);
        // The merge is reversible for a fortnight: an FYI card, not a question.
        await sql.query(
          `insert into review_items (user_id, kind, evidence_ref, application_id, candidates, expires_at)
           values ($1, 'merge_undo', $2, $3, $4, now() + make_interval(days => $5))`,
          [
            signal.user_id,
            signal.evidence_ref,
            dup.keep,
            JSON.stringify([{ merged: dup.merge, kept: dup.keep, cosine: dup.cos }]),
            RESOLVER.MERGE_UNDO_DAYS,
          ],
        );
        M.resolverDecisions.inc({ decision: 'merged' });
        ctx.log.info({
          outcome: 'merged',
          application_id: dup.keep,
          cosine: Number(dup.cos.toFixed(3)),
        });
      }
    }

    const events: PendingEvent[] = eventsForSignal(signal, applicationId);
    for (const ev of events) await publish(sql, QUEUES.event, ev);

    // A cancelled invite is ambiguous by nature: rescheduling, or over? The
    // stage claim is withdrawn automatically; the question is asked once.
    if (signal.intent === 'interview_cancelled') {
      await sql.query(
        `insert into review_items (user_id, kind, evidence_ref, application_id, excerpt)
         values ($1, 'unknown_intent', $2, $3, $4)
         on conflict do nothing`,
        [
          signal.user_id,
          signal.evidence_ref,
          applicationId,
          'An interview was cancelled. Rescheduling, or has this one ended?',
        ],
      );
    }

    await sql.query(
      `update seen_messages set outcome = $3, processed_at = now()
        where mailbox_id = $1 and provider_message_id = $2`,
      [signal.mailbox_id, signal.provider_message_id, events.length ? 'placed' : 'dropped'],
    );

    ctx.log.info({
      provider_message_id: signal.provider_message_id,
      application_id: applicationId,
      decision: decision.kind,
      outcome: events.length ? 'placed' : 'no_event',
      intent: signal.intent,
      count: events.length,
      cosine: 'cosine' in decision ? Number(decision.cosine.toFixed(3)) : undefined,
    });
  });

  return async () => {
    await consumer.stop();
  };
});
