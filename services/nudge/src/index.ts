import pg from 'pg';
import { publish, QUEUES } from '@loop/queue';
import { startService } from '@loop/runtime';
import {
  evaluateNudges,
  rankAndCap,
  StageTable,
  suggestionKey,
  type AppSnapshot,
  type DeadlineSnapshot,
  type InterviewSnapshot,
  type PendingNotification,
  type StageDef,
  type Suggestion,
} from '@loop/domain';

/**
 * The nudge service.
 *
 * "A scheduled fold over the log, not a chat." pg_cron sends a tick every
 * fifteen minutes; everything decided here is a pure function of a snapshot,
 * which is why the budget can be tested without a clock.
 */

interface UserRow {
  id: string;
  tz: string;
}

async function snapshotFor(pool: pg.Pool, userId: string) {
  const client = await pool.connect();
  try {
    await client.query('select set_config($1,$2,true)', ['loop.user_id', userId]);

    const apps = await client.query<AppSnapshot & { company: string }>(
      `select a.id, c.canonical_name as company, a.role_title, a.current_stage,
              a.status, a.last_signal_at, a.awaiting_them, a.last_user_action_at,
              a.went_dormant_at
         from applications a
         join companies c on c.id = a.company_id
        where a.user_id = $1 and a.merged_into_id is null`,
      [userId],
    );

    const interviews = await client.query<InterviewSnapshot>(
      `select id, application_id, stage, starts_at from interviews
        where user_id = $1 and cancelled_at is null and starts_at > now()`,
      [userId],
    );

    const deadlines = await client.query<DeadlineSnapshot>(
      `select application_id, kind, due_at, source from deadlines
        where user_id = $1 and met_at is null and due_at > now()`,
      [userId],
    );

    const dwell = await client.query<{ stage: string; p50_days: number; p75_days: number; n: number }>(
      `select stage, p50_days, p75_days, n from stage_dwell_in where user_id = $1`,
      [userId],
    );

    const stageDefs = await client.query<StageDef>(
      `select key, label, phase, depth, stale_after_days from stage_defs where user_id = $1`,
      [userId],
    );

    const open = await client.query<{ key: string }>(
      `select key from suggestions
        where user_id = $1 and dismissed_at is null and acted_at is null
          and (expires_at is null or expires_at > now())`,
      [userId],
    );

    // Below the §11 gate the percentile is not a number worth trusting, so it
    // is reported as absent and the rule falls back to stale_after_days.
    const gated = new Map(dwell.rows.filter((r) => r.n >= 5).map((r) => [r.stage, r]));

    return {
      applications: apps.rows,
      interviews: interviews.rows,
      deadlines: deadlines.rows,
      p75: (stage: string) => gated.get(stage)?.p75_days ?? null,
      p50: (stage: string) => {
        const v = gated.get(stage)?.p50_days;
        return v === undefined ? null : Math.round(v);
      },
      openOrIssued: new Set(open.rows.map((r) => r.key)),
      stages: new StageTable(stageDefs.rows.length ? stageDefs.rows : undefined),
    };
  } finally {
    client.release();
  }
}

async function persist(pool: pg.Pool, userId: string, suggestions: Suggestion[]): Promise<Suggestion[]> {
  const client = await pool.connect();
  const fresh: Suggestion[] = [];
  try {
    await client.query('select set_config($1,$2,true)', ['loop.user_id', userId]);
    for (const s of suggestions) {
      const res = await client.query(
        `insert into suggestions (user_id, key, rule, application_ids, payload, expires_at)
         values ($1,$2,$3,$4,$5,$6)
         on conflict (user_id, key) do nothing`,
        [userId, s.key, s.rule, s.applicationIds, JSON.stringify(s), s.expiresAt],
      );
      if (res.rowCount) fresh.push(s);
    }

    // Expire what is no longer true, so "one per application per rule, ever,
    // unless it expired and re-triggered" holds across restarts.
    await client.query(
      `update suggestions set expires_at = now()
        where user_id = $1 and acted_at is null and dismissed_at is null
          and expires_at is not null and expires_at < now()`,
      [userId],
    );
  } finally {
    client.release();
  }
  return fresh;
}

await startService({ name: 'nudge', healthPort: 9106 }, async (ctx) => {
  const tick = async (): Promise<void> => {
    const users = await ctx.pool.query<UserRow>('select id, tz from users');
    for (const user of users.rows) {
      const snap = await snapshotFor(ctx.pool, user.id);
      const all = evaluateNudges({
        now: new Date(),
        applications: snap.applications,
        interviews: snap.interviews,
        deadlines: snap.deadlines,
        p75DwellDays: snap.p75,
        p50DwellDays: snap.p50,
        openOrIssued: snap.openOrIssued,
        stages: snap.stages,
      });

      // The cap is a *display* budget: everything true is stored, at most three
      // are surfaced, and the notifier applies its own separate push budget.
      const fresh = await persist(ctx.pool, user.id, all);
      const surfaced = rankAndCap(all);

      for (const s of fresh) {
        if (!s.pushable) continue; // let_it_go is weekly-digest only, never a push
        const notification: PendingNotification = {
          user_id: user.id,
          suggestion_key: s.key,
          rule: s.rule,
          title: s.title,
          body: s.body,
          url: `/suggestions/${encodeURIComponent(s.key)}`,
          bypasses_budget: s.bypassesBudget,
        };
        await publish(ctx.pool, QUEUES.notify, notification);
      }

      if (all.length) {
        ctx.log.info({
          user_id: user.id,
          outcome: 'evaluated',
          count: all.length,
          depth: surfaced.length,
        });
      }
    }
  };

  const listener = new pg.Client({ connectionString: ctx.config.databaseUrl });
  await listener.connect();
  await listener.query('listen loop_nudge');
  listener.on('notification', () => void tick().catch((err: Error) =>
    ctx.log.error({ msg: 'tick failed', error: err.message }),
  ));

  // A belt-and-braces interval: if pg_cron is misconfigured the product must
  // still nudge, just less punctually.
  const timer = setInterval(() => void tick().catch(() => undefined), 15 * 60_000);
  await tick();

  return async () => {
    clearInterval(timer);
    await listener.end().catch(() => undefined);
  };
});
