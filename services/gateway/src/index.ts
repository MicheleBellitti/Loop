import { randomBytes, createHash } from 'node:crypto';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import Fastify, { type FastifyReply, type FastifyRequest } from 'fastify';
import cookie from '@fastify/cookie';
import fastifyStatic from '@fastify/static';
import rateLimit from '@fastify/rate-limit';
import pg from 'pg';
import { z } from 'zod';
import { createPool, withUser, withUserReadOnly } from '@loop/db';
import { publish, QUEUES, queueDepth, totalDeadLetters, oldestMessageAgeSeconds } from '@loop/queue';
import { createLogger, loadConfig, renderMetrics } from '@loop/runtime';
import { FRESHNESS, REVIEW_EXCERPT_MAX_CHARS, type PendingEvent } from '@loop/domain';
import {
  csrfMatches,
  createSession,
  destroySession,
  loadSession,
  SESSION_COOKIE,
  sessionCookieOptions,
  verifyPassword,
  type Session,
} from './auth.js';
import { registerAuthRoutes } from './routes-auth.js';
import { buildStats, buildToday, getApplication, listApplications, loadUser, mailboxHealth, type Period } from './queries.js';
import { registerMailboxRoutes } from './routes-mailbox.js';
import { registerSse, broadcast } from './sse.js';
import { buildDraft } from './drafts.js';
import { fetchPostingHtml, parsePosting } from './ssrf.js';

/**
 * The gateway: one BFF, the single ingress, reads projections only.
 *
 * Everything user-facing runs inside `withUser`, which opens a transaction and
 * sets the tenant GUC so row-level security does the filtering. No handler in
 * this file writes `where user_id = $1` by hand.
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const config = loadConfig();
const log = createLogger('gateway');
const pool = createPool({
  applicationName: 'loop-gateway',
  // The gateway is the single ingress, so it is the one process whose death
  // takes the product offline rather than just stalling a queue.
  onError: (err) => log.error({ msg: 'idle database client failed', error: String(err) }),
});

const app = Fastify({ logger: false, trustProxy: true, bodyLimit: 1_000_000 });

/**
 * The error handler, registered before anything else.
 *
 * Ordering is load-bearing and not obvious: registering `@fastify/static` after
 * the routes causes a *later* `setErrorHandler` to be silently ignored, and
 * every 500 then falls through to Fastify's default — which serialises the
 * exception message. That leaked SQL text and Postgres error codes to the
 * client until an end-to-end run surfaced it. Setting it first is the fix, and
 * `services/gateway/src/errors.test.ts` is the regression.
 */
app.setErrorHandler((err: unknown, _req, reply) => {
  const status = (err as { statusCode?: number }).statusCode ?? 500;
  const message = err instanceof Error ? err.message : 'unknown error';
  log.error({ msg: 'request failed', error: message, code: String(status) });
  // The client only ever parses `code`. An internal error string is exactly the
  // kind of thing that leaks a query, a path or a column name.
  return reply.code(status).send({
    error: {
      code: status === 404 ? 'not_found' : status === 500 ? 'internal' : 'bad_request',
      message: status === 500 ? 'something failed' : message,
    },
  });
});

await app.register(cookie);
await app.register(rateLimit, {
  global: false,
  max: 300,
  timeWindow: '1 minute',
  keyGenerator: (req) => (req as FastifyRequest & { session?: Session }).session?.userId ?? req.ip,
});

declare module 'fastify' {
  interface FastifyRequest {
    session?: Session;
  }
}

// ── security headers ───────────────────────────────────────────────────────
// "CSP without unsafe-inline; the client is built, not templated."
app.addHook('onSend', async (_req, reply, payload) => {
  reply.header(
    'content-security-policy',
    [
      "default-src 'self'",
      "script-src 'self'",
      "style-src 'self'",
      "img-src 'self' data:",
      "font-src 'self' data:",
      "connect-src 'self'",
      "frame-ancestors 'none'",
      "base-uri 'none'",
      "form-action 'self'",
    ].join('; '),
  );
  reply.header('x-content-type-options', 'nosniff');
  reply.header('referrer-policy', 'no-referrer');
  reply.header('permissions-policy', 'geolocation=(), microphone=(), camera=()');
  if (config.publicOrigin.startsWith('https:')) {
    reply.header('strict-transport-security', 'max-age=31536000; includeSubDomains');
  }
  return payload;
});

// ── session and CSRF ───────────────────────────────────────────────────────
const PUBLIC_PATHS = new Set([
  '/health',
  '/health/deep',
  '/metrics',
  '/api/auth/state',
  '/api/auth/login/options',
  '/api/auth/login/verify',
  '/api/auth/recover',
  '/api/gmail/push',
]);

app.addHook('preHandler', async (req, reply) => {
  const url = req.url.split('?')[0] ?? '';
  if (!url.startsWith('/api') && url !== '/metrics') return; // static assets
  if (PUBLIC_PATHS.has(url)) return;

  const token = req.cookies[SESSION_COOKIE];
  const session = await loadSession(pool, token);
  if (!session) {
    return reply.code(401).send({ error: { code: 'unauthenticated', message: 'sign in first' } });
  }
  req.session = session;

  // CSRF on every mutation. SameSite=Lax already blocks cross-site form posts;
  // the token covers the rest.
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    const presented = req.headers['x-csrf-token'];
    if (!csrfMatches(session, Array.isArray(presented) ? presented[0] : presented)) {
      return reply.code(403).send({ error: { code: 'csrf', message: 'missing or invalid CSRF token' } });
    }
  }
});

const userOf = (req: FastifyRequest): string => {
  if (!req.session) throw new Error('handler ran without a session');
  return req.session.userId;
};

const fail = (reply: FastifyReply, code: string, message: string, status = 400, field?: string) =>
  reply.code(status).send({ error: { code, message, field } });

// ── auth, mailboxes, SSE ───────────────────────────────────────────────────
await registerAuthRoutes(app, { pool, config, log });
await registerMailboxRoutes(app, { pool, config, log });
registerSse(app, { pool, config, log });

// ── Today ──────────────────────────────────────────────────────────────────
app.get('/api/today', async (req) =>
  withUserReadOnly(userOf(req), async (sql) => {
    const user = await loadUser(sql, userOf(req));
    return buildToday(sql, user);
  }, pool),
);

// ── Applications ───────────────────────────────────────────────────────────
const ListQuery = z.object({
  phase: z.string().optional(),
  status: z.string().optional(),
  // Defaulted in `listApplications`, not here, so the mobile client — which
  // sends no query at all — gets the same board as the desktop one.
  activity: z.enum(['open', 'active', 'stale', 'closed', 'all']).optional(),
  sort: z.enum(['last_signal', 'stage_depth', 'company']).optional(),
  cursor: z.string().uuid().optional(),
  limit: z.coerce.number().int().min(1).max(200).optional(),
});

app.get('/api/applications', async (req, reply) => {
  const parsed = ListQuery.safeParse(req.query);
  if (!parsed.success) return fail(reply, 'bad_query', parsed.error.issues[0]!.message);
  return withUserReadOnly(userOf(req), async (sql) => {
    const user = await loadUser(sql, userOf(req));
    return listApplications(sql, user, parsed.data);
  }, pool);
});

const IdParam = z.object({ id: z.string().uuid() });

app.get('/api/applications/:id', async (req, reply) => {
  const params = IdParam.safeParse(req.params);
  if (!params.success) return fail(reply, 'bad_id', 'that is not an application id', 400, 'id');
  const { id } = params.data;
  const result = await withUserReadOnly(userOf(req), async (sql) => {
    const user = await loadUser(sql, userOf(req));
    return getApplication(sql, user, id);
  }, pool);
  if (!result) return fail(reply, 'not_found', 'no such application', 404);
  return result;
});

const QuickAdd = z.union([
  z.object({ posting_url: z.string().url() }),
  z.object({
    company: z.string().min(1),
    role: z.string().min(1),
    channel: z.enum(['linkedin', 'indeed', 'career_page', 'referral', 'recruiter', 'other']),
    applied_at: z.string().datetime().optional(),
    posting_url: z.string().url().optional(),
  }),
]);

app.post('/api/applications', { config: { rateLimit: { max: 60, timeWindow: '1 minute' } } }, async (req, reply) => {
  const parsed = QuickAdd.safeParse(req.body);
  if (!parsed.success) return fail(reply, 'bad_body', parsed.error.issues[0]!.message);
  const userId = userOf(req);

  // "With a URL, metadata is fetched best-effort and never blocks the 201."
  let meta = { company: null as string | null, role: null as string | null, location: null as string | null, ats_vendor: null as string | null, comp: null as { min_minor: number; max_minor: number | null; currency: string } | null };
  const postingUrl = 'posting_url' in parsed.data ? parsed.data.posting_url : undefined;
  if (postingUrl) {
    try {
      meta = parsePosting(await fetchPostingHtml(postingUrl));
    } catch (err) {
      log.info({ msg: 'posting fetch failed, continuing', error: (err as Error).message });
    }
  }

  const company = 'company' in parsed.data ? parsed.data.company : (meta.company ?? 'Unknown');
  const role = 'role' in parsed.data ? parsed.data.role : (meta.role ?? 'Unknown role');
  const channel = 'channel' in parsed.data ? parsed.data.channel : 'career_page';
  const appliedAt = 'applied_at' in parsed.data && parsed.data.applied_at
    ? new Date(parsed.data.applied_at)
    : new Date();

  const id = await withUser(userId, async (sql) => {
    const companyRow = await sql.query<{ id: string }>(
      `insert into companies (canonical_name) values ($1)
       on conflict (lower(canonical_name), coalesce(domain,'')) do update set canonical_name = excluded.canonical_name
       returning id`,
      [company],
    );
    const created = await sql.query<{ id: string }>(
      `insert into applications
         (user_id, company_id, role_title, current_stage, current_phase, manually_created, confidence, location)
       values ($1,$2,$3,'applied','sent',true,1.0,$4)
       returning id`,
      [userId, companyRow.rows[0]!.id, role, meta.location],
    );
    const applicationId = created.rows[0]!.id;

    // The row exists; the event that *justifies* it goes through the pipeline
    // like everything else, so the projection is never written by two authors.
    const pending: PendingEvent = {
      user_id: userId,
      application_id: applicationId,
      event: {
        type: 'applied',
        occurred_at: appliedAt.toISOString(),
        confidence: 1.0,
        to_stage: 'applied',
        rung: 4,
        payload: { channel, posting_url: postingUrl ?? null, role_title: role },
      },
      source: { channel, posting_url: postingUrl ?? null, ats_vendor: meta.ats_vendor, is_first_touch: true },
    };
    await publish(sql, QUEUES.event, pending);

    if (meta.comp) {
      await sql.query(
        `insert into comp_offers (user_id, application_id, kind, min_minor, max_minor, currency)
         values ($1,$2,'posted_range',$3,$4,$5)`,
        [userId, applicationId, meta.comp.min_minor, meta.comp.max_minor, meta.comp.currency],
      );
    }
    return applicationId;
  }, pool);

  return reply.code(201).send({ id, company, role, channel });
});

const Correction = z.object({
  field: z.enum(['stage', 'status', 'role_title', 'seniority', 'location', 'work_mode', 'channel', 'applied_at', 'comp_expectation']),
  to: z.union([z.string(), z.number(), z.object({ minor: z.number(), currency: z.string().length(3) })]),
});

app.post('/api/applications/:id/correct', async (req, reply) => {
  const params = IdParam.safeParse(req.params);
  if (!params.success) return fail(reply, 'bad_id', 'that is not an application id', 400, 'id');
  const { id } = params.data;
  const parsed = Correction.safeParse(req.body);
  if (!parsed.success) return fail(reply, 'bad_body', parsed.error.issues[0]!.message);
  const userId = userOf(req);

  await withUser(userId, async (sql) => {
    const current = await sql.query<{ current_stage: string; status: string }>(
      `select current_stage, status from applications where id = $1`,
      [id],
    );
    if (!current.rowCount) throw Object.assign(new Error('not found'), { statusCode: 404 });
    const from = parsed.data.field === 'stage' ? current.rows[0]!.current_stage : current.rows[0]!.status;

    // "Correcting writes a human_corrected event at confidence 1.0 that the
    // agent will not overwrite." It is the only stage-write the client has.
    const pending: PendingEvent = {
      user_id: userId,
      application_id: id,
      event: {
        type: 'human_corrected',
        occurred_at: new Date().toISOString(),
        confidence: 1.0,
        rung: 4,
        payload: { field: parsed.data.field, from, to: parsed.data.to },
      },
    };
    await publish(sql, QUEUES.event, pending);
    await sql.query(`update applications set last_user_action_at = now() where id = $1`, [id]);
  }, pool);

  return { ok: true };
});

const Archive = z.object({ as: z.enum(['dormant', 'withdrawn']) });
const ArchiveMany = z.object({ ids: z.array(z.string().uuid()).min(1).max(200), as: z.enum(['dormant', 'withdrawn']).default('dormant') });

async function archive(userId: string, ids: string[], as: 'dormant' | 'withdrawn'): Promise<void> {
  await withUser(userId, async (sql) => {
    for (const id of ids) {
      const pending: PendingEvent = {
        user_id: userId,
        application_id: id,
        event:
          as === 'withdrawn'
            ? { type: 'withdrawn', occurred_at: new Date().toISOString(), confidence: 1.0, rung: 4, payload: {} }
            : {
                type: 'went_silent',
                occurred_at: new Date().toISOString(),
                confidence: 1.0,
                rung: 4,
                payload: { threshold_used: 'archived_by_user' },
              },
      };
      await publish(sql, QUEUES.event, pending);
      await sql.query(`update applications set last_user_action_at = now() where id = $1`, [id]);
    }
  }, pool);
}

app.post('/api/applications/:id/archive', async (req, reply) => {
  const params = IdParam.safeParse(req.params);
  if (!params.success) return fail(reply, 'bad_id', 'that is not an application id', 400, 'id');
  const parsed = Archive.safeParse(req.body);
  if (!parsed.success) return fail(reply, 'bad_body', parsed.error.issues[0]!.message);
  await archive(userOf(req), [params.data.id], parsed.data.as);
  return { ok: true };
});

app.post('/api/applications/archive', async (req, reply) => {
  const parsed = ArchiveMany.safeParse(req.body);
  if (!parsed.success) return fail(reply, 'bad_body', parsed.error.issues[0]!.message);
  await archive(userOf(req), parsed.data.ids, parsed.data.as);
  return { ok: true, count: parsed.data.ids.length };
});

// ── Statistics ─────────────────────────────────────────────────────────────
app.get('/api/stats', async (req) => {
  const period = ((req.query as { period?: string }).period ?? '12m') as Period;
  return withUserReadOnly(userOf(req), async (sql) => {
    const user = await loadUser(sql, userOf(req));
    return buildStats(sql, user, ['90d', '12m', 'all'].includes(period) ? period : '12m');
  }, pool);
});

// ── Review queue ───────────────────────────────────────────────────────────
app.get('/api/review', async (req) =>
  withUserReadOnly(userOf(req), async (sql) => {
    const res = await sql.query(
      `select id, kind, evidence_ref, excerpt, candidates, application_id, created_at, expires_at
         from review_items where user_id = $1 and resolved_at is null order by created_at`,
      [userOf(req)],
    );
    return { items: res.rows };
  }, pool),
);

const ReviewAnswer = z.object({
  choice: z.union([
    z.object({ kind: z.literal('application'), application_id: z.string().uuid() }),
    z.object({ kind: z.literal('new_application') }),
    z.object({ kind: z.literal('intent'), intent: z.string(), agree: z.boolean() }),
    z.object({ kind: z.literal('undo_merge') }),
  ]),
  learn: z.boolean().default(true),
});

app.post('/api/review/:id', async (req, reply) => {
  const params = IdParam.safeParse(req.params);
  if (!params.success) return fail(reply, 'bad_id', 'that is not a review item id', 400, 'id');
  const { id } = params.data;
  const parsed = ReviewAnswer.safeParse(req.body);
  if (!parsed.success) return fail(reply, 'bad_body', parsed.error.issues[0]!.message);
  const userId = userOf(req);

  await withUser(userId, async (sql) => {
    const res = await sql.query<{ kind: string; candidates: unknown; evidence_ref: string }>(
      `select kind, candidates, evidence_ref from review_items where id = $1 and resolved_at is null`,
      [id],
    );
    const item = res.rows[0];
    if (!item) throw Object.assign(new Error('not found'), { statusCode: 404 });

    if (parsed.data.choice.kind === 'undo_merge') {
      const [merge] = item.candidates as Array<{ merged: string; kept: string }>;
      if (merge) {
        await sql.query(`update applications set merged_into_id = null where id = $1`, [merge.merged]);
        const pending: PendingEvent = {
          user_id: userId,
          application_id: merge.kept,
          event: {
            type: 'human_corrected',
            occurred_at: new Date().toISOString(),
            confidence: 1.0,
            rung: 4,
            payload: { field: 'merge', from: 'merged', to: 'split' },
          },
        };
        await publish(sql, QUEUES.event, pending);
      }
    }

    // "Each answer is written back as a rule, so this queue shrinks over time
    // instead of growing." What survives the item is the structural pattern
    // only — never the excerpt, never a name. decisions.md D6.
    await sql.query(
      `update review_items
          set resolved_at = now(), resolution = $2, excerpt = null,
              learned_pattern = case when $3 then $4::jsonb else null end
        where id = $1`,
      [
        id,
        JSON.stringify(parsed.data.choice),
        parsed.data.learn,
        JSON.stringify({ kind: item.kind, answer: parsed.data.choice.kind }),
      ],
    );
  }, pool);

  return { ok: true };
});

// ── Suggestions ────────────────────────────────────────────────────────────
app.get('/api/suggestions', async (req) =>
  withUserReadOnly(userOf(req), async (sql) => {
    const res = await sql.query(
      `select key, rule, application_ids, payload, expires_at from suggestions
        where user_id = $1 and acted_at is null and dismissed_at is null
          and (snoozed_until is null or snoozed_until < now())
          and (expires_at is null or expires_at > now())
        order by created_at desc limit 3`,
      [userOf(req)],
    );
    return { suggestions: res.rows };
  }, pool),
);

for (const [action, column] of [
  ['act', 'acted_at'],
  ['dismiss', 'dismissed_at'],
] as const) {
  app.post(`/api/suggestions/:key/${action}`, async (req) => {
    const { key } = req.params as { key: string };
    await withUser(userOf(req), async (sql) => {
      await sql.query(`update suggestions set ${column} = now() where user_id = $1 and key = $2`, [
        userOf(req),
        key,
      ]);
    }, pool);
    return { ok: true };
  });
}

app.post('/api/suggestions/:key/snooze', async (req) => {
  const { key } = req.params as { key: string };
  await withUser(userOf(req), async (sql) => {
    // "Later" removes the card for the session; the rule re-triggers per §12.
    await sql.query(
      `update suggestions set snoozed_until = now() + interval '1 day' where user_id = $1 and key = $2`,
      [userOf(req), key],
    );
  }, pool);
  return { ok: true };
});

/**
 * The draft for one application, with no suggestion behind it.
 *
 * "Draft follow-up" sits on every application record, but the only draft route
 * there was keyed on a suggestion — so the button worked on the two or three
 * applications a nudge rule happened to have fired for, and 404ed on the rest.
 * The composition is identical; what changes is that the caller may name the
 * application directly.
 */
app.get('/api/applications/:id/draft', async (req, reply) => {
  const params = IdParam.safeParse(req.params);
  if (!params.success) return fail(reply, 'bad_id', 'that is not an application id', 400, 'id');
  const draft = await withUserReadOnly(userOf(req), (sql) => draftFor(sql, params.data.id), pool);
  if (!draft) return fail(reply, 'not_found', 'no such application', 404);
  return { ...draft, can_send: false, note: 'Loop holds a read-only scope, so it cannot send this.' };
});

app.get('/api/suggestions/:key/draft', async (req, reply) => {
  const { key } = req.params as { key: string };
  const draft = await withUserReadOnly(userOf(req), async (sql) => {
    const s = await sql.query<{ application_ids: string[] }>(
      `select application_ids from suggestions where user_id = $1 and key = $2`,
      [userOf(req), key],
    );
    const applicationId = s.rows[0]?.application_ids?.[0];
    if (!applicationId) return null;
    return draftFor(sql, applicationId);
  }, pool);

  if (!draft) return fail(reply, 'not_found', 'no draft for that suggestion', 404);
  // Generated, never sent. There is no code path in this repository that can
  // deliver it, and `npm run lint:no-send-path` asserts as much.
  return { ...draft, can_send: false, note: 'Loop holds a read-only scope, so it cannot send this.' };
});

async function draftFor(sql: pg.PoolClient, applicationId: string) {
  const a = await sql.query<{
    company: string;
    current_stage: string;
    last_signal_at: Date | null;
    label: string | null;
  }>(
    `select c.canonical_name as company, a.current_stage, a.last_signal_at, sd.label
       from applications a
       join companies c on c.id = a.company_id
       left join stage_defs sd on sd.user_id = a.user_id and sd.key = a.current_stage
      where a.id = $1 and a.merged_into_id is null`,
    [applicationId],
  );
  const row = a.rows[0];
  if (!row) return null;

  const lastEvent = await sql.query<{ payload: Record<string, unknown>; type: string; to_stage: string | null }>(
    `select payload, type, to_stage from application_events
      where application_id = $1 order by occurred_at desc limit 1`,
    [applicationId],
  );

  return buildDraft({
    company: row.company,
    contactName: null,
    stageLabel: row.label ?? row.current_stage,
    lastEventLabel: row.label ?? null,
    daysQuiet: row.last_signal_at
      ? Math.floor((Date.now() - row.last_signal_at.getTime()) / 86_400_000)
      : 0,
    language: (lastEvent.rows[0]?.payload?.language as 'it' | 'en') ?? 'en',
    threadMessageId: (lastEvent.rows[0]?.payload?.thread_id as string) ?? null,
    toAddress: null,
    subject: null,
  });
}

// ── Push subscriptions ─────────────────────────────────────────────────────
const Subscription = z.object({
  endpoint: z.string().url(),
  keys: z.object({ p256dh: z.string(), auth: z.string() }),
});

app.post('/api/push/subscribe', async (req, reply) => {
  const parsed = Subscription.safeParse(req.body);
  if (!parsed.success) return fail(reply, 'bad_body', parsed.error.issues[0]!.message);
  await withUser(userOf(req), async (sql) => {
    await sql.query(
      `insert into push_subscriptions (user_id, endpoint, p256dh, auth) values ($1,$2,$3,$4)
       on conflict (user_id, endpoint) do update set p256dh = excluded.p256dh, auth = excluded.auth`,
      [userOf(req), parsed.data.endpoint, parsed.data.keys.p256dh, parsed.data.keys.auth],
    );
  }, pool);
  return { ok: true };
});

app.get('/api/push/key', async () => ({ public_key: config.vapid.publicKey }));

// ── Export and erasure ─────────────────────────────────────────────────────
app.get('/api/export', async (req, reply) => {
  const query = req.query as { format?: string; ids?: string };
  const format = (query.format ?? 'json') as 'json' | 'csv';
  const userId = userOf(req);
  // A selection, for the bulk bar's "export these" — which used to link at the
  // flat route and hand back the whole account while sitting under a line that
  // said how many were selected. Ids that are not ids are dropped rather than
  // rejected: the fallback is everything, which is never wrong, only broader.
  const selected = (query.ids ?? '')
    .split(',')
    .map((id) => id.trim())
    .filter((id) => /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id))
    .slice(0, 200);

  // "Complete event log, machine-readable, no rate limit for the owner."
  const data = await withUserReadOnly(userId, async (sql) => {
    const applications = await sql.query(
      selected.length
        ? `select a.*, c.canonical_name as company from applications a
             join companies c on c.id = a.company_id
            where a.user_id = $1 and a.id = any($2::uuid[]) order by a.created_at`
        : `select a.*, c.canonical_name as company from applications a
             join companies c on c.id = a.company_id where a.user_id = $1 order by a.created_at`,
      selected.length ? [userId, selected] : [userId],
    );
    const events = await sql.query(
      `select * from application_events where user_id = $1 order by occurred_at, id`,
      [userId],
    );
    const sources = await sql.query(`select * from sources where user_id = $1`, [userId]);
    const interviews = await sql.query(`select * from interviews where user_id = $1`, [userId]);
    const offers = await sql.query(`select * from comp_offers where user_id = $1`, [userId]);
    return {
      applications: applications.rows,
      events: events.rows,
      sources: sources.rows,
      interviews: interviews.rows,
      comp_offers: offers.rows,
    };
  }, pool);

  if (format === 'csv') {
    reply.header('content-type', 'text/csv; charset=utf-8');
    reply.header('content-disposition', 'attachment; filename="loop-applications.csv"');
    const rows = data.applications as Array<Record<string, unknown>>;
    const headers = rows[0] ? Object.keys(rows[0]) : [];
    const escape = (v: unknown): string => `"${String(v ?? '').replace(/"/g, '""')}"`;
    return [headers.join(','), ...rows.map((r) => headers.map((h) => escape(r[h])).join(','))].join('\n');
  }

  reply.header('content-type', 'application/json; charset=utf-8');
  reply.header('content-disposition', 'attachment; filename="loop-export.json"');
  return data;
});

app.delete('/api/account', async (req, reply) => {
  const body = z.object({ confirm: z.literal('DELETE') }).safeParse(req.body);
  if (!body.success) return fail(reply, 'confirm_required', 'send {"confirm":"DELETE"}');
  const userId = userOf(req);
  const receipt = randomBytes(9).toString('base64url');

  // One function, because the cascade needs the append-only escape hatch and
  // the queue purge must not be forgotten — a deletion that leaves your mail
  // sitting in a queue is not an erasure.
  await withUser(userId, (sql) => sql.query('select erase_user($1)', [userId]), pool);

  log.info({ user_id: userId, msg: 'account deleted', code: receipt });
  return reply.code(200).send({ ok: true, receipt });
});

// ── Health and metrics ─────────────────────────────────────────────────────
app.get('/health', async () => ({ ok: true }));

app.get('/health/deep', async (reply) => {
  const depths: Record<string, number> = {};
  let oldest = 0;
  for (const q of Object.values(QUEUES)) {
    depths[q] = await queueDepth(pool, q);
    oldest = Math.max(oldest, await oldestMessageAgeSeconds(pool, q));
  }
  const dead = await totalDeadLetters(pool);
  const mailboxes = await pool.query<{ last_ok_at: Date | null }>(
    `select last_ok_at from mailbox_accounts`,
  );
  const stalest = mailboxes.rows.reduce<number | null>((acc, r) => {
    if (!r.last_ok_at) return acc;
    const hours = (Date.now() - r.last_ok_at.getTime()) / 3_600_000;
    return acc === null ? hours : Math.max(acc, hours);
  }, null);

  const model = config.model.baseUrl
    ? await fetch(`${config.model.baseUrl.replace(/\/$/, '')}/models`, { signal: AbortSignal.timeout(2000) })
        .then((r) => (r.ok ? 'reachable' : `http ${r.status}`))
        .catch(() => 'unreachable')
    : 'disabled';

  return {
    ok: dead === 0 && oldest < FRESHNESS.OLDEST_UNPROCESSED_ALERT_MIN * 60,
    queues: depths,
    oldest_unprocessed_seconds: oldest,
    dead_letters: dead,
    mailbox_staleness_hours: stalest,
    // Named per component, so a stalled model never reads as "the app is
    // broken" — this is what failure state F4 renders.
    components: {
      template_rules: 'running',
      calendar_detection: 'running',
      local_model: model,
    },
  };
});

app.get('/metrics', async (req, reply) => {
  reply.header('content-type', 'text/plain; version=0.0.4');
  return renderMetrics();
});

// ── the built client ───────────────────────────────────────────────────────
await app.register(fastifyStatic, {
  root: join(HERE, '..', '..', '..', 'client', 'dist'),
  prefix: '/',
  index: 'index.html',
  // Wildcard on: `false` snapshots the directory at boot, so a client rebuilt
  // without restarting the gateway serves 404s for its own new asset hashes.
  wildcard: true,
});

/**
 * The SPA fallback, with a limit.
 *
 * Serving index.html for *every* miss is the usual one-liner and it is wrong:
 * a request for a missing `/assets/index-abc.js` then gets HTML with a 200, the
 * browser refuses it on MIME grounds, and the user sees a blank page with a
 * message that names neither the file nor the cause. Only navigations get the
 * shell; anything that asked for a file gets an honest 404.
 */
app.setNotFoundHandler(async (req, reply) => {
  if (req.url.startsWith('/api')) {
    return reply.code(404).send({ error: { code: 'not_found', message: 'no such endpoint' } });
  }
  const path = req.url.split('?')[0] ?? '';
  const accept = req.headers.accept ?? '';
  if (/\.[a-z0-9]{2,5}$/i.test(path) || !accept.includes('text/html')) {
    return reply.code(404).send({ error: { code: 'not_found', message: 'no such file' } });
  }
  return reply.sendFile('index.html');
});


await app.listen({ port: config.port, host: '0.0.0.0' });
log.info({ msg: 'listening', endpoint: config.publicOrigin });

for (const signal of ['SIGTERM', 'SIGINT'] as const) {
  process.on(signal, () => {
    void (async () => {
      await app.close();
      await pool.end();
      process.exit(0);
    })();
  });
}

export { app, broadcast, verifyPassword, createSession, destroySession, sessionCookieOptions, mailboxHealth, createHash, pg, REVIEW_EXCERPT_MAX_CHARS };
