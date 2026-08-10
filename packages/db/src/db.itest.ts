import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import pg from 'pg';
import { createPool, withUser } from './client.js';
import { migrate } from './migrate.js';
import { appendEvent, projectApplication } from './events.js';
import { rebuildAll, snapshotApplications } from './rebuild.js';
import { initCrypto, generateDek, seal, open, wrapDek, unwrapDek, rewrapDek, redact } from './crypto.js';

/**
 * The invariants §17 asks a real database to prove.
 *
 * "Integration tests run against a real Postgres… The interesting bugs live in
 * SQL and in parsing, and both need the real thing." Start one with
 * `npm run test:db:up`, which sets TEST_DATABASE_URL for you.
 */

const CONNECTION = process.env.TEST_DATABASE_URL ?? 'postgres://loop:loop@localhost:55432/loop';

let pool: pg.Pool;
let userA: string;
let userB: string;
let companyId: string;

beforeAll(async () => {
  await migrate({ connectionString: CONNECTION });
  pool = createPool({ connectionString: CONNECTION, max: 4 });
  await initCrypto();

  // A clean tenant per run: the deletion test asserts a cascade leaves nothing,
  // so it needs to own everything it touches.
  const a = await pool.query<{ id: string }>(
    `insert into users (email) values ($1) returning id`,
    [`a-${Date.now()}@example.com`],
  );
  const b = await pool.query<{ id: string }>(
    `insert into users (email) values ($1) returning id`,
    [`b-${Date.now()}@example.com`],
  );
  userA = a.rows[0]!.id;
  userB = b.rows[0]!.id;
  await pool.query('select seed_stage_defs($1), seed_stage_defs($2)', [userA, userB]);

  const c = await pool.query<{ id: string }>(
    `insert into companies (canonical_name, domain) values ($1, $2)
     on conflict (lower(canonical_name), coalesce(domain,'')) do update set canonical_name = excluded.canonical_name
     returning id`,
    ['Satispay', `satispay-${Date.now()}.com`],
  );
  companyId = c.rows[0]!.id;
}, 120_000);

afterAll(async () => {
  await pool?.query(`delete from users where id = any($1)`, [[userA, userB]]).catch(() => undefined);
  await pool?.end();
});

/**
 * Every tenant-scoped call goes through a service role.
 *
 * A superuser bypasses row-level security entirely — policies, FORCE and all —
 * so a test that connects as the owner proves nothing about isolation. These
 * are the same roles the compose file gives each container.
 */
const AS_RESOLVER = { role: 'loop_resolver' };
const AS_PIPELINE = { role: 'loop_pipeline' };
const AS_GATEWAY = { role: 'loop_gateway' };

async function createApplication(userId: string, role = 'Backend Engineer'): Promise<string> {
  return withUser(
    userId,
    async (sql) => {
      const res = await sql.query<{ id: string }>(
        `insert into applications (user_id, company_id, role_title, current_stage, current_phase)
         values ($1,$2,$3,'applied','sent') returning id`,
        [userId, companyId, role],
      );
      return res.rows[0]!.id;
    },
    pool,
    AS_RESOLVER,
  );
}

describe('row-level security', () => {
  it('one tenant cannot see another, and no tenant sees nothing', async () => {
    const id = await createApplication(userA);

    const seenByA = await withUser(userA, (sql) => sql.query('select id from applications where id = $1', [id]), pool, AS_GATEWAY);
    expect(seenByA.rowCount).toBe(1);

    const seenByB = await withUser(userB, (sql) => sql.query('select id from applications where id = $1', [id]), pool, AS_GATEWAY);
    expect(seenByB.rowCount).toBe(0);

    // The GUC unset — a migration, a cron job, a maintenance session. §04's
    // one-liner would have thrown here; the two-argument form returns null and
    // the policy simply matches nothing.
    const anonymous = await withUser('', (sql) => sql.query('select id from applications where id = $1', [id]), pool, AS_GATEWAY);
    expect(anonymous.rowCount).toBe(0);
  });

  it('a tenant cannot write into another tenant', async () => {
    await expect(
      withUser(
        userB,
        (sql) =>
          sql.query(
            `insert into applications (user_id, company_id, role_title, current_stage, current_phase)
             values ($1,$2,'Sneaky','applied','sent')`,
            [userA, companyId],
          ),
        pool,
        AS_RESOLVER,
      ),
    ).rejects.toThrow();
  });
});

describe('the event log is append-only', () => {
  it('refuses an UPDATE and a DELETE', async () => {
    const id = await createApplication(userA);
    const eventId = await withUser(
      userA,
      (sql) =>
        appendEvent(sql, {
          userId: userA,
          applicationId: id,
          type: 'applied',
          occurredAt: new Date('2026-07-02T09:00:00Z'),
          confidence: 1,
          rung: 4,
        }),
      pool,
      AS_PIPELINE,
    );
    expect(eventId).toBeTruthy();

    await expect(
      withUser(userA, (sql) => sql.query(`update application_events set confidence = 0.1 where id = $1`, [eventId]), pool, AS_PIPELINE),
    ).rejects.toThrow(/append-only|permission denied/);

    await expect(
      withUser(userA, (sql) => sql.query(`delete from application_events where id = $1`, [eventId]), pool, AS_PIPELINE),
    ).rejects.toThrow(/append-only|permission denied/);
  });
});

describe('idempotency', () => {
  it('delivering the same event twice produces one row', async () => {
    const id = await createApplication(userA);
    const input = {
      userId: userA,
      applicationId: id,
      type: 'acknowledged' as const,
      occurredAt: new Date('2026-07-02T09:04:00Z'),
      confidence: 0.99,
      evidenceRef: 'msg-abc',
      rung: 1 as const,
    };

    const first = await withUser(userA, (sql) => appendEvent(sql, input), pool, AS_PIPELINE);
    const second = await withUser(userA, (sql) => appendEvent(sql, input), pool, AS_PIPELINE);

    expect(first).toBeTruthy();
    // The second delivery is a no-op, which is how the pipeline knows not to
    // buzz the user's phone a second time.
    expect(second).toBeNull();

    const count = await withUser(
      userA,
      (sql) => sql.query(`select count(*)::int as n from application_events where application_id = $1`, [id]),
      pool,
      AS_GATEWAY,
    );
    expect((count.rows[0] as { n: number }).n).toBe(1);
  });

  it('holds for human-authored events, whose evidence_ref is null', async () => {
    // The §04 unique index is defeated by NULLs unless it is NULLS NOT
    // DISTINCT — and every correction has a null evidence_ref.
    const id = await createApplication(userA);
    const input = {
      userId: userA,
      applicationId: id,
      type: 'human_corrected' as const,
      occurredAt: new Date('2026-07-03T09:00:00Z'),
      confidence: 1,
      rung: 4 as const,
      payload: { field: 'stage' as const, from: 'applied', to: 'hr_call' },
    };
    await withUser(userA, (sql) => appendEvent(sql, input), pool, AS_PIPELINE);
    const second = await withUser(userA, (sql) => appendEvent(sql, input), pool, AS_PIPELINE);
    expect(second).toBeNull();
  });
});

describe('the projection rebuilds from the log alone', () => {
  it('is byte-identical after a drop and rebuild', async () => {
    const id = await createApplication(userA, 'Platform Engineer');

    const events = [
      { type: 'applied' as const, at: '2026-06-12T08:00:00Z', conf: 1, rung: 4 as const, to: 'applied' },
      { type: 'acknowledged' as const, at: '2026-06-13T08:00:00Z', conf: 0.99, rung: 1 as const, to: 'acknowledged' },
      { type: 'stage_advanced' as const, at: '2026-07-01T08:00:00Z', conf: 0.95, rung: 1 as const, to: 'hr_call' },
      { type: 'stage_advanced' as const, at: '2026-07-24T08:00:00Z', conf: 0.9, rung: 2 as const, to: 'onsite_loop' },
    ];

    await withUser(
      userA,
      async (sql) => {
        for (const [i, e] of events.entries()) {
          await appendEvent(sql, {
            userId: userA,
            applicationId: id,
            type: e.type,
            occurredAt: new Date(e.at),
            confidence: e.conf,
            toStage: e.to,
            rung: e.rung,
            evidenceRef: `m-${i}`,
          });
        }
        await projectApplication(sql, userA, id);
      },
      pool,
      AS_PIPELINE,
    );

    // Scoped to this application: the invariant is that what the pipeline
    // maintained incrementally equals what a rebuild from the log produces.
    const before = await withUser(userA, (sql) => snapshotApplications(sql, userA, [id]), pool, AS_GATEWAY);
    await withUser(userA, (sql) => rebuildAll(sql, userA), pool, AS_PIPELINE);
    const after = await withUser(userA, (sql) => snapshotApplications(sql, userA, [id]), pool, AS_GATEWAY);

    expect(after).toEqual(before);

    const state = (after as Array<{ id: string; current_stage: string; current_phase: string }>).find(
      (r) => r.id === id,
    );
    // And the fold advanced past the 0.99 acknowledgement, which the literal
    // §05 rule could not have done.
    expect(state?.current_stage).toBe('onsite_loop');
    expect(state?.current_phase).toBe('interviewing');
  });
});

describe('the queue', () => {
  it('hides a claimed message for its visibility timeout, then releases it', async () => {
    await pool.query(`select mq.send('raw_message', $1::jsonb)`, [JSON.stringify({ probe: true })]);

    const first = await pool.query(`select msg_id from mq.read('raw_message', 60, 10)`);
    expect(first.rowCount).toBeGreaterThan(0);

    const second = await pool.query(`select msg_id from mq.read('raw_message', 60, 10)`);
    expect(second.rowCount).toBe(0);

    const msgId = (first.rows[0] as { msg_id: string }).msg_id;
    // A zero-second timeout makes it visible again immediately: this is the
    // retry path, and it must actually retry.
    await pool.query(`update mq.messages set vt = now() where msg_id = $1`, [msgId]);
    const retried = await pool.query(`select msg_id, read_ct from mq.read('raw_message', 60, 10)`);
    expect(retried.rowCount).toBe(1);
    expect((retried.rows[0] as { read_ct: number }).read_ct).toBe(2);

    await pool.query(`select mq.delete('raw_message', $1::bigint)`, [msgId]);
  });
});

describe('erasure', () => {
  it('leaves no row in any table for that user', async () => {
    const doomed = await pool.query<{ id: string }>(
      `insert into users (email) values ($1) returning id`,
      [`doomed-${Date.now()}@example.com`],
    );
    const userId = doomed.rows[0]!.id;
    await pool.query('select seed_stage_defs($1)', [userId]);

    const appId = await createApplication(userId);
    await withUser(
      userId,
      (sql) =>
        appendEvent(sql, {
          userId,
          applicationId: appId,
          type: 'applied',
          occurredAt: new Date(),
          confidence: 1,
          rung: 4,
        }),
      pool,
      AS_PIPELINE,
    );
    await pool.query(`select mq.send('event_pending', $1::jsonb)`, [JSON.stringify({ user_id: userId })]);

    // The one function that owns erasure: the cascade needs the append-only
    // escape hatch, and the queue purge must not be forgotten.
    await pool.query('select erase_user($1)', [userId]);

    const tables = [
      'applications', 'application_events', 'sources', 'stage_defs', 'mailbox_accounts',
      'seen_messages', 'interviews', 'comp_offers', 'deadlines', 'review_items',
      'suggestions', 'push_subscriptions', 'notifications_sent', 'company_aliases',
      'credentials', 'auth_secrets', 'sessions', 'consents',
    ];
    for (const table of tables) {
      const res = await pool.query<{ n: string }>(
        `select count(*)::text as n from ${table} where user_id = $1`,
        [userId],
      );
      expect({ table, n: Number(res.rows[0]!.n) }).toEqual({ table, n: 0 });
    }

    const queued = await pool.query<{ n: string }>(
      `select count(*)::text as n from mq.messages where message->>'user_id' = $1`,
      [userId],
    );
    expect(Number(queued.rows[0]!.n)).toBe(0);
  });
});

describe('envelope encryption', () => {
  it('seals, opens, and survives a KEK rotation', () => {
    const kek = Buffer.alloc(32, 7);
    const newKek = Buffer.alloc(32, 9);
    const dek = generateDek();

    const wrapped = wrapDek(dek, kek);
    expect(unwrapDek(wrapped, kek)).toEqual(dek);

    const secret = seal('a-refresh-token', dek);
    expect(open(secret, dek).toString('utf8')).toBe('a-refresh-token');

    // The runbook promises rotation works; an untested rotation is a promise,
    // not a procedure.
    const rewrapped = rewrapDek(wrapped, kek, newKek);
    expect(unwrapDek(rewrapped, newKek)).toEqual(dek);
    expect(() => unwrapDek(rewrapped, kek)).toThrow();
  });

  it('a stolen ciphertext is unreadable with the wrong key', () => {
    const dek = generateDek();
    const other = generateDek();
    const sealed = seal('token', dek);
    expect(() => open(sealed, other)).toThrow();
  });

  it('the log serialiser redacts anything that looks like a secret', () => {
    const line = redact({
      mailbox_id: 'abc',
      refresh_token: 'ya29.secret',
      nested: { authorization: 'Bearer x', app_password: 'hunter2' },
    });
    expect(JSON.stringify(line)).not.toContain('ya29');
    expect(JSON.stringify(line)).not.toContain('hunter2');
    expect(JSON.stringify(line)).toContain('abc');
  });
});
