import pg from 'pg';
import { startService } from '@loop/runtime';
import { CONNECTOR } from '@loop/domain';
import { GoogleClient, type MailboxRow } from '@loop/google';
import { backfill, renewWatch, syncCalendar, syncHistory, type SyncDeps } from './sync.js';

/**
 * The connector service.
 *
 * Long-lived on purpose: a renewed Gmail watch and a LISTEN channel both want a
 * process that stays up, which is the reason the whole design is one small box
 * rather than a set of functions.
 *
 * Push arrives at the gateway (the single ingress) which verifies the
 * Google-signed JWT and forwards a bare wake-up over LISTEN/NOTIFY. Nothing
 * from the outside world reaches this process directly, and the notification
 * carries no payload worth trusting — it just says "look again".
 */

await startService({ name: 'connector', healthPort: 9105 }, async (ctx) => {
  const google = new GoogleClient({
    clientId: ctx.config.google.clientId ?? '',
    clientSecret: ctx.config.google.clientSecret ?? '',
  });
  const deps: SyncDeps = { pool: ctx.pool, google, log: ctx.log };
  const watchFailures = new Map<string, number>();
  let stopping = false;

  const mailboxes = async (provider = 'gmail'): Promise<MailboxRow[]> => {
    const res = await ctx.pool.query<MailboxRow>(
      `select * from mailbox_accounts where provider = $1 and status in ('ok','error')`,
      [provider],
    );
    return res.rows;
  };

  const syncAll = async (): Promise<void> => {
    for (const mailbox of await mailboxes('gmail')) {
      try {
        await syncHistory(deps, mailbox);
      } catch (err) {
        ctx.log.error({ mailbox_id: mailbox.id, msg: 'sync failed', error: (err as Error).message });
      }
    }
    for (const mailbox of await mailboxes('google_calendar')) {
      try {
        await syncCalendar(deps, mailbox);
      } catch (err) {
        ctx.log.error({ mailbox_id: mailbox.id, msg: 'calendar sync failed', error: (err as Error).message });
      }
    }
  };

  // ── the wake-up channel ──────────────────────────────────────────────────
  const listener = new pg.Client({ connectionString: ctx.config.databaseUrl });
  await listener.connect();
  await listener.query('listen loop_connector');
  listener.on('notification', (msg) => {
    ctx.log.debug({ msg: 'wake', reason: msg.payload ?? 'push' });
    void syncAll();
  });

  // ── watch renewal, every 24 h ────────────────────────────────────────────
  const renewAll = async (): Promise<void> => {
    const topic = ctx.config.google.pubsubTopic;
    if (!topic) return;
    for (const mailbox of await mailboxes('gmail')) {
      const before = watchFailures.get(mailbox.id) ?? 0;
      const { failures } = await renewWatch(deps, mailbox, topic, before);
      watchFailures.set(mailbox.id, failures);
    }
  };

  const renewTimer = setInterval(
    () => void renewAll(),
    CONNECTOR.WATCH_RENEW_EVERY_HOURS * 3_600_000,
  );

  // ── the polling fallback ─────────────────────────────────────────────────
  // "GOOGLE_PUBSUB_TOPIC absent → connector falls back to polling every 5 min."
  // It also runs when a watch has failed its three renewals, so a lapsed
  // subscription degrades to slow rather than to silent — which is the whole
  // point of failure state F2.
  const pollTimer = setInterval(() => {
    if (!stopping) void syncAll();
  }, CONNECTOR.POLL_INTERVAL_MS);

  await renewAll();
  await syncAll();

  // ── first-scan requests ──────────────────────────────────────────────────
  const backfillListener = new pg.Client({ connectionString: ctx.config.databaseUrl });
  await backfillListener.connect();
  await backfillListener.query('listen loop_backfill');
  backfillListener.on('notification', (msg) => {
    void (async () => {
      try {
        const { mailbox_id: mailboxId, months } = JSON.parse(msg.payload ?? '{}') as {
          mailbox_id?: string;
          months?: number;
        };
        if (!mailboxId) return;
        const res = await ctx.pool.query<MailboxRow>(`select * from mailbox_accounts where id = $1`, [
          mailboxId,
        ]);
        const mailbox = res.rows[0];
        if (!mailbox) return;
        ctx.log.info({ mailbox_id: mailboxId, msg: 'first scan starting', count: months ?? 12 });
        await backfill(deps, mailbox, months ?? 12);
      } catch (err) {
        ctx.log.error({ msg: 'backfill failed', error: (err as Error).message });
      }
    })();
  });

  return async () => {
    stopping = true;
    clearInterval(renewTimer);
    clearInterval(pollTimer);
    await listener.end().catch(() => undefined);
    await backfillListener.end().catch(() => undefined);
  };
});
