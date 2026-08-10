import { createConsumer, QUEUES } from '@loop/queue';
import { M, startService } from '@loop/runtime';
import {
  isQuietHour,
  localParts,
  NOTIFICATIONS,
  nextDeliverableAt,
  type PendingNotification,
} from '@loop/domain';
import { sendPush } from './push.js';

/**
 * The notifier.
 *
 * "The tracker's job is to lower the ambient anxiety of a job search, and a
 * tracker that notifies you nine times raises it." Every rule below is a
 * refusal to send:
 *
 *   · at most one push per calendar day, in the user's own timezone
 *   · nothing between 21:00 and 08:00
 *   · rejections and dormancy are never pushed at all
 *   · one suggestion per application per rule, ever
 *
 * The deadline rule is the single exception, and it is the §19 question the
 * owner answered yes to: it may pass the cap and it may break quiet hours,
 * because it is the only alert whose silence has a cost you cannot undo.
 */

const NEVER_PUSHED = new Set(['rejected', 'went_silent', 'let_it_go']);

await startService({ name: 'notifier', healthPort: 9107 }, async (ctx) => {
  const consumer = createConsumer(ctx.pool, {
    queue: QUEUES.notify,
    batch: 10,
    concurrency: 2,
    log: (line) => ctx.log.debug(line),
  });

  void consumer.start<PendingNotification>(async (n, _env, sql) => {
    await sql.query('select set_config($1,$2,true)', ['loop.user_id', n.user_id]);

    if (NEVER_PUSHED.has(n.rule)) {
      ctx.log.info({ user_id: n.user_id, rule: n.rule, outcome: 'suppressed', reason: 'never pushed' });
      return;
    }

    const userRes = await sql.query<{ tz: string }>('select tz from users where id = $1', [n.user_id]);
    const tz = userRes.rows[0]?.tz ?? 'Europe/Rome';
    const now = new Date();
    const local = localParts(now, tz);
    const localDate = `${local.year}-${String(local.month).padStart(2, '0')}-${String(local.day).padStart(2, '0')}`;

    // ── the daily cap ──────────────────────────────────────────────────────
    if (!n.bypasses_budget) {
      const sentToday = await sql.query<{ n: string }>(
        `select count(*)::text as n from notifications_sent
          where user_id = $1 and local_date = $2::date`,
        [n.user_id, localDate],
      );
      if (Number(sentToday.rows[0]?.n ?? '0') >= NOTIFICATIONS.MAX_PUSH_PER_DAY) {
        ctx.log.info({ user_id: n.user_id, rule: n.rule, outcome: 'suppressed', reason: 'daily cap' });
        return;
      }
    }

    // ── quiet hours ────────────────────────────────────────────────────────
    if (isQuietHour(now, tz, ctx.config.quietHours)) {
      if (!(n.bypasses_budget && NOTIFICATIONS.DEADLINE_BREAKS_QUIET_HOURS)) {
        // Deferred, not dropped: re-queued with a delay so it arrives at 08:00
        // rather than being lost because the user was asleep.
        const deliverAt = nextDeliverableAt(now, tz, ctx.config.quietHours);
        const delaySeconds = Math.ceil((deliverAt.getTime() - now.getTime()) / 1000);
        await sql.query('select mq.send($1, $2, $3)', [
          QUEUES.notify,
          JSON.stringify(n),
          delaySeconds,
        ]);
        ctx.log.info({
          user_id: n.user_id,
          rule: n.rule,
          outcome: 'deferred',
          reason: 'quiet hours',
        });
        return;
      }
    }

    const subs = await sql.query<{ endpoint: string; p256dh: string; auth: string }>(
      `select endpoint, p256dh, auth from push_subscriptions where user_id = $1`,
      [n.user_id],
    );
    if (subs.rowCount === 0) {
      ctx.log.info({ user_id: n.user_id, rule: n.rule, outcome: 'no_subscription' });
      return;
    }

    let delivered = 0;
    for (const sub of subs.rows) {
      const result = await sendPush(
        { endpoint: sub.endpoint, keys: { p256dh: sub.p256dh, auth: sub.auth } },
        { title: n.title, body: n.body, url: n.url, tag: n.suggestion_key },
        ctx.config.vapid,
      );
      if (result === 'gone') {
        // The browser told us this subscription is dead. Keeping it would mean
        // retrying forever against an endpoint that no longer exists.
        await sql.query(`delete from push_subscriptions where user_id = $1 and endpoint = $2`, [
          n.user_id,
          sub.endpoint,
        ]);
      } else if (result === 'ok') {
        delivered += 1;
      }
    }

    if (delivered > 0) {
      await sql.query(
        `insert into notifications_sent (user_id, rule, suggestion_key, local_date)
         values ($1,$2,$3,$4::date)`,
        [n.user_id, n.rule, n.suggestion_key, localDate],
      );
      M.notificationsSent.inc({ rule: n.rule });
    }

    ctx.log.info({ user_id: n.user_id, rule: n.rule, outcome: delivered ? 'sent' : 'failed', count: delivered });
  });

  return async () => {
    await consumer.stop();
  };
});
