import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import pg from 'pg';
import { createPool, migrate, withUser } from '@loop/db';
import { buildStats, buildToday, listApplications, loadUser, type UserContext } from './queries.js';

/**
 * The read models, against a real database.
 *
 * `activity` is one SQL expression evaluated per row, and it decides what the
 * board shows by default, what the live counter counts and which applications
 * hold the statistics gates shut. Its ladder is unit-tested in
 * `packages/domain/src/activity.test.ts`; this asserts that the SQL saying the
 * same thing agrees with it — which no amount of typechecking can, because
 * every interesting part of it is an interval, a lateral join and a coalesce.
 */

const CONNECTION = process.env.TEST_DATABASE_URL ?? 'postgres://loop:loop@localhost:55432/loop';
const AS_GATEWAY = { role: 'loop_gateway' };
const AS_RESOLVER = { role: 'loop_resolver' };
const AS_PIPELINE = { role: 'loop_pipeline' };
const DAY = 86_400_000;

let pool: pg.Pool;
let userId: string;
let companyId: string;
let user: UserContext;

const daysAgo = (n: number): Date => new Date(Date.now() - n * DAY);

interface Seed {
  role: string;
  stage: string;
  phase: string;
  status?: string;
  quietDays?: number | null;
  presumedClosed?: boolean;
}

async function seed(app: Seed): Promise<string> {
  return withUser(
    userId,
    async (sql) => {
      const res = await sql.query<{ id: string }>(
        `insert into applications
           (user_id, company_id, role_title, current_stage, current_phase, status,
            applied_at, last_signal_at, presumed_closed)
         values ($1,$2,$3,$4,$5,$6,$7,$8,$9) returning id`,
        [
          userId,
          companyId,
          app.role,
          app.stage,
          app.phase,
          app.status ?? 'live',
          daysAgo((app.quietDays ?? 0) + 30),
          app.quietDays === null ? null : daysAgo(app.quietDays ?? 1),
          app.presumedClosed ?? false,
        ],
      );
      return res.rows[0]!.id;
    },
    pool,
    AS_RESOLVER,
  );
}

/** Only the pipeline may write an interview — 003 grants it to nobody else. */
async function book(applicationId: string, inDays: number): Promise<void> {
  await withUser(
    userId,
    (sql) =>
      sql.query(
        `insert into interviews (user_id, application_id, stage, starts_at) values ($1,$2,$3,$4)`,
        [userId, applicationId, 'technical', new Date(Date.now() + inDays * DAY)],
      ),
    pool,
    AS_PIPELINE,
  );
}

const activityOfRow = async (role: string): Promise<string> => {
  const { rows } = await withUser(
    userId,
    (sql) => listApplications(sql, user, { activity: 'all', limit: 200 }),
    pool,
    AS_GATEWAY,
  );
  const row = rows.find((r) => r.role === role);
  if (!row) throw new Error(`no row for ${role}`);
  return row.activity;
};

beforeAll(async () => {
  await migrate({ connectionString: CONNECTION });
  pool = createPool({ connectionString: CONNECTION, max: 4 });

  const u = await pool.query<{ id: string }>(`insert into users (email) values ($1) returning id`, [
    `activity-${Date.now()}@example.com`,
  ]);
  userId = u.rows[0]!.id;
  await pool.query('select seed_stage_defs($1)', [userId]);

  const c = await pool.query<{ id: string }>(
    `insert into companies (canonical_name, domain) values ($1,$2)
     on conflict (lower(canonical_name), coalesce(domain,'')) do update set canonical_name = excluded.canonical_name
     returning id`,
    ['Acme', `acme-${Date.now()}.com`],
  );
  companyId = c.rows[0]!.id;

  await seed({ role: 'Moving', stage: 'hr_call', phase: 'screening', quietDays: 3 });
  await seed({ role: 'Quiet', stage: 'hr_call', phase: 'screening', quietDays: 40 });
  await seed({ role: 'Silent since applying', stage: 'acknowledged', phase: 'sent', quietDays: 75 });
  await seed({ role: 'Still young', stage: 'acknowledged', phase: 'sent', quietDays: 45 });
  await seed({ role: 'Silent after a call', stage: 'hr_call', phase: 'screening', quietDays: 100 });
  await seed({ role: 'Waiting on me', stage: 'take_home', phase: 'screening', quietDays: 200 });
  const booked = await seed({ role: 'Booked', stage: 'technical', phase: 'interviewing', quietDays: 200 });
  await book(booked, 6);
  await seed({ role: 'Swept', stage: 'hr_call', phase: 'screening', quietDays: 95, presumedClosed: true });
  await seed({ role: 'Rejected', stage: 'hr_call', phase: 'screening', status: 'rejected', quietDays: 5 });
  await seed({ role: 'Never heard from', stage: 'applied', phase: 'sent', quietDays: null });

  user = await withUser(userId, (sql) => loadUser(sql, userId), pool, AS_GATEWAY);
}, 180_000);

afterAll(async () => {
  await pool?.query('delete from users where id = $1', [userId]).catch(() => undefined);
  await pool?.end();
});

describe('activity, in SQL', () => {
  it('agrees with the domain ladder rung by rung', async () => {
    expect(await activityOfRow('Moving')).toBe('active');
    expect(await activityOfRow('Quiet')).toBe('stale');
    // 75 days with nobody ever replying is past the sent-phase threshold…
    expect(await activityOfRow('Silent since applying')).toBe('closed');
    // …and 45 is not.
    expect(await activityOfRow('Still young')).toBe('stale');
    // Past a reply it takes ninety.
    expect(await activityOfRow('Silent after a call')).toBe('closed');
    // The ball is in your court: silence there is a task, not a verdict.
    expect(await activityOfRow('Waiting on me')).toBe('active');
    // A date in the diary outranks any amount of quiet.
    expect(await activityOfRow('Booked')).toBe('active');
    expect(await activityOfRow('Swept')).toBe('closed');
    expect(await activityOfRow('Rejected')).toBe('closed');
    // Nothing has ever arrived on it; that is not silence.
    expect(await activityOfRow('Never heard from')).toBe('active');
  });

  it('defaults the board to what is still happening, and can be asked for the rest', async () => {
    const open = await withUser(userId, (sql) => listApplications(sql, user, {}), pool, AS_GATEWAY);
    const roles = open.rows.map((r) => r.role).sort();
    expect(roles).toEqual(
      ['Booked', 'Moving', 'Never heard from', 'Quiet', 'Still young', 'Waiting on me'].sort(),
    );

    const closed = await withUser(
      userId,
      (sql) => listApplications(sql, user, { activity: 'closed' }),
      pool,
      AS_GATEWAY,
    );
    expect(closed.rows.map((r) => r.role).sort()).toEqual(
      ['Rejected', 'Silent after a call', 'Silent since applying', 'Swept'].sort(),
    );

    // The tabs' labels come from one query, and they add up.
    expect(open.counts.open).toBe(6);
    expect(open.counts.closed).toBe(4);
    expect(open.counts.all).toBe(10);
    expect(open.counts.active + open.counts.stale).toBe(open.counts.open);
  });

  it('filters before the limit rather than after it', async () => {
    // Two rows asked for, two rows back — a filter applied to the fetched page
    // would return whichever of the two happened to be open.
    const page = await withUser(
      userId,
      (sql) => listApplications(sql, user, { activity: 'open', limit: 2 }),
      pool,
      AS_GATEWAY,
    );
    expect(page.rows).toHaveLength(2);
    expect(page.rows.every((r) => r.activity !== 'closed')).toBe(true);
  });

  it('sorts by every key the board offers', async () => {
    for (const sort of ['last_signal', 'stage_depth', 'company'] as const) {
      const res = await withUser(
        userId,
        (sql) => listApplications(sql, user, { sort, activity: 'all' }),
        pool,
        AS_GATEWAY,
      );
      expect(res.rows).toHaveLength(10);
    }
  });
});

describe('the counters', () => {
  it('count what is moving, not what nothing has closed', async () => {
    const today = await withUser(userId, (sql) => buildToday(sql, user), pool, AS_GATEWAY);
    // Moving, Waiting on me, Booked, Never heard from.
    expect(today.counters.live).toBe(4);
    // Quiet, Still young.
    expect(today.counters.quiet).toBe(2);
    expect(today.counters.closed).toBe(4);
    expect(today.counters.interviewing).toBe(1);
  });
});

describe('the statistics', () => {
  it('judge the cohort by activity, so silence counts as closed', async () => {
    // The projection the metrics read is refreshed by the pipeline; refresh it
    // here so the assertions are about this test's rows.
    await withUser(userId, (sql) => sql.query('select refresh_projections()'), pool, AS_PIPELINE);

    const stats = await withUser(userId, (sql) => buildStats(sql, user, 'all'), pool, AS_GATEWAY);

    // Four closed: one rejection and three that simply stopped. Under the old
    // `status <> 'live'` rule this was one, and every gate stayed shut.
    expect(stats.ratios[0]!.note).toContain('4 closed');
    // Ghosted is closed-without-a-no: the three silent ones, not the rejection.
    expect(stats.ghost.numerator).toBe(3);
    expect(stats.ghost.denominator).toBe(4);

    // The new sections are present and shaped as the client reads them.
    expect(stats.outcomes.open + stats.outcomes.rejected + stats.outcomes.ghosted).toBe(10);
    expect(Array.isArray(stats.by_month)).toBe(true);
    for (const month of stats.by_month) {
      expect(month.label).toMatch(/^[A-Z][a-z]{2} \d{2}$/);
      expect(month.applied).toBeGreaterThanOrEqual(month.replied);
    }
  });

  it('rounds median dwell rather than shipping a float to the browser', async () => {
    const stats = await withUser(userId, (sql) => buildStats(sql, user, 'all'), pool, AS_GATEWAY);
    for (const stage of stats.time_in_stage) {
      expect(stage.days).toBe(Math.round(stage.days * 10) / 10);
      expect(stage.display).toMatch(/^[\d.]+ days?$/);
    }
  });
});
