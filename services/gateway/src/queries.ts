import type pg from 'pg';
import {
  buildHeadline,
  computeFlag,
  dateEyebrow,
  daysQuiet,
  displayStage,
  formatDays,
  formatPercent,
  GATES,
  quietLabel,
  ratio,
  RATIO_MATURITY_DAYS,
  SILENCE,
  StageTable,
  type Activity,
  type AppStatus,
  type DomainEvent,
  type Metric,
  type StageDef,
} from '@loop/domain';

/**
 * Read models.
 *
 * "The client never computes a statistic, never derives a stage, and never
 * decides whether an application is dormant — all of that arrives precomputed,
 * including `days_quiet` and each row's `flag`." Everything the interface shows
 * is assembled here.
 */

export interface UserContext {
  id: string;
  tz: string;
  locale: string;
  displayCurrency: string;
  stages: StageTable;
}

export async function loadUser(sql: pg.PoolClient, userId: string): Promise<UserContext> {
  const u = await sql.query<{ tz: string; locale: string; display_currency: string }>(
    `select tz, locale, display_currency from users where id = $1`,
    [userId],
  );
  const defs = await sql.query<StageDef>(
    `select key, label, phase, depth, stale_after_days from stage_defs where user_id = $1 order by depth`,
    [userId],
  );
  return {
    id: userId,
    tz: u.rows[0]?.tz ?? 'Europe/Rome',
    locale: u.rows[0]?.locale ?? 'en-GB',
    displayCurrency: u.rows[0]?.display_currency ?? 'EUR',
    stages: new StageTable(defs.rows.length ? defs.rows : undefined),
  };
}

export interface ApplicationRow {
  id: string;
  company: string;
  role: string;
  stage: string;
  display_stage: string;
  phase: string;
  status: AppStatus;
  channel: string | null;
  applied_at: string | null;
  last_signal_at: string | null;
  days_quiet: number | null;
  quiet_label: string;
  flag: string;
  flag_kind: string;
  closed: boolean;
  /** Whether this is still happening. See `activity.ts` for the ladder. */
  activity: Activity;
  next_interview_at: string | null;
  needs_review: boolean;
  confidence: number;
}

/**
 * `activityOf`, as one SQL expression.
 *
 * The same ladder, in the same order, over a whole table — the numbers come
 * from the same constants so the two cannot drift apart on a threshold change,
 * and `activity.ts` carries the argument for each rung. It is SQL and not a
 * post-filter in TypeScript because the board, the counters and every ratio ask
 * this question with a `where` on it, and a filter applied after `limit` would
 * quietly return short pages.
 *
 * It reads `sd`, `d` and `nx` from the select below.
 */
const ACTIVITY_SQL = `
  case
    when a.status <> 'live' then 'closed'
    when nx.starts_at is not null then 'active'
    when a.presumed_closed then 'closed'
    when a.current_stage in (${SILENCE.SKIP_STAGES.map((s) => `'${s}'`).join(',')}) then 'active'
    when a.last_signal_at is null then 'active'
    when a.last_signal_at < now() - make_interval(days => case when a.current_phase = 'sent'
           then ${SILENCE.NO_REPLY_CLOSED_DAYS} else ${SILENCE.PRESUMED_CLOSED_DAYS} end) then 'closed'
    when a.last_signal_at < now() - make_interval(days =>
           ceil(greatest(coalesce(sd.stale_after_days, 21), coalesce(2 * d.p90_days, 0)))::int) then 'stale'
    else 'active'
  end`;

const APPLICATION_SELECT = `
  select a.id, c.canonical_name as company, a.role_title, a.current_stage, a.current_phase,
         a.status, a.applied_at, a.last_signal_at, a.needs_review, a.presumed_closed, a.confidence,
         s.channel,
         sd.stale_after_days,
         sd.depth,
         d.p90_days,
         nx.starts_at as next_interview_at,
         ${ACTIVITY_SQL} as activity,
         (select min(due_at) from deadlines dl
           where dl.application_id = a.id and dl.met_at is null and dl.due_at > now()) as deadline_at,
         (select min(decide_by) from comp_offers co
           where co.application_id = a.id and co.decide_by is not null) as decide_by
    from applications a
    join companies c on c.id = a.company_id
    left join sources s on s.application_id = a.id and s.is_first_touch
    left join stage_defs sd on sd.user_id = a.user_id and sd.key = a.current_stage
    left join stage_dwell_in d on d.user_id = a.user_id and d.stage = a.current_stage and d.n >= 5
    left join lateral (
      select min(i.starts_at) as starts_at from interviews i
       where i.application_id = a.id and i.cancelled_at is null and i.starts_at > now()
    ) nx on true
   where a.user_id = $1 and a.merged_into_id is null`;

interface RawApplicationRow {
  id: string;
  company: string;
  role_title: string;
  current_stage: string;
  current_phase: string;
  status: AppStatus;
  applied_at: Date | null;
  last_signal_at: Date | null;
  needs_review: boolean;
  presumed_closed: boolean;
  confidence: string;
  channel: string | null;
  stale_after_days: number | null;
  p90_days: number | null;
  next_interview_at: Date | null;
  activity: Activity;
  deadline_at: Date | null;
  decide_by: Date | null;
}

function toRow(r: RawApplicationRow, user: UserContext, now: Date): ApplicationRow {
  const quiet = daysQuiet(now, r.last_signal_at);
  const flag = computeFlag({
    now,
    tz: user.tz,
    locale: user.locale,
    status: r.status,
    deadlineAt: r.deadline_at,
    decideBy: r.decide_by,
    lastSignalAt: r.last_signal_at,
    quietThresholdDays: r.p90_days ?? r.stale_after_days,
  });
  return {
    id: r.id,
    company: r.company,
    role: r.role_title,
    stage: r.current_stage,
    display_stage: displayStage(r.status, r.current_stage, user.stages, {
      presumedClosed: r.presumed_closed,
    }),
    phase: r.current_phase,
    status: r.status,
    channel: r.channel,
    applied_at: r.applied_at?.toISOString() ?? null,
    last_signal_at: r.last_signal_at?.toISOString() ?? null,
    days_quiet: quiet,
    quiet_label: quietLabel(quiet),
    flag: flag.text,
    flag_kind: flag.kind,
    // `closed` is the row's own outcome and dims it in the table; `activity` is
    // whether anything is still happening, which is not the same question and
    // is the one the default filter and the live counter ask.
    closed: r.status !== 'live',
    activity: r.activity,
    next_interview_at: r.next_interview_at?.toISOString() ?? null,
    needs_review: r.needs_review,
    confidence: Number(r.confidence),
  };
}

export interface ListOptions {
  phase?: string;
  status?: string;
  /** open (the default), active, stale, closed, all. */
  activity?: string;
  sort?: 'last_signal' | 'stage_depth' | 'company';
  cursor?: string;
  limit?: number;
}

/**
 * What each `activity` filter admits.
 *
 * `open` is the default and it is the product's answer to "what am I actually
 * doing": everything not written off, quiet ones included, because a quiet
 * application is the one that most needs you. History is a deliberate second
 * request rather than the thing you wade through to find today's work.
 */
const ACTIVITY_FILTERS: Record<string, readonly Activity[] | null> = {
  open: ['active', 'stale'],
  active: ['active'],
  stale: ['stale'],
  closed: ['closed'],
  all: null,
};
const DEFAULT_ACTIVITY = 'open';

export async function listApplications(
  sql: pg.PoolClient,
  user: UserContext,
  opts: ListOptions,
): Promise<{ rows: ApplicationRow[]; next_cursor: string | null; counts: Record<string, number> }> {
  const params: unknown[] = [user.id];
  let where = '';
  if (opts.phase && opts.phase !== 'all') {
    params.push(opts.phase);
    where += ` and a.current_phase = $${params.length}`;
  }
  if (opts.status) {
    params.push(opts.status);
    where += ` and a.status = $${params.length}`;
  }
  if (opts.cursor) {
    params.push(opts.cursor);
    where += ` and a.id > $${params.length}`;
  }

  // `in` rather than `??`: `all` maps to null *meaning* "no predicate", and a
  // nullish fallback reads that as "not a filter I know" and quietly serves the
  // default instead — so asking for the whole history returned the six open
  // ones. The integration test in `queries.itest.ts` is the one that noticed.
  const key = opts.activity && opts.activity in ACTIVITY_FILTERS ? opts.activity : DEFAULT_ACTIVITY;
  const wanted = ACTIVITY_FILTERS[key];
  // Filtered in SQL rather than after the fact: applied after `limit` this
  // would hand back a short page and call it the whole pipeline.
  const activityWhere = wanted ? ` and t.activity in (${wanted.map((a) => `'${a}'`).join(',')})` : '';

  const order =
    opts.sort === 'company'
      ? 't.company asc, t.id asc'
      : opts.sort === 'stage_depth'
        ? 'coalesce(t.depth, 0) desc, t.id asc'
        : 't.last_signal_at desc nulls last, t.id asc';

  const limit = Math.min(opts.limit ?? 100, 200);
  params.push(limit + 1);

  // The select is wrapped so `activity` — an expression, and not one worth
  // repeating in a `where` — can be filtered and ordered by name.
  const res = await sql.query<RawApplicationRow>(
    `select t.* from (${APPLICATION_SELECT}${where}) t
      where true${activityWhere}
      order by ${order} limit $${params.length}`,
    params,
  );

  const now = new Date();
  const rows = res.rows.slice(0, limit).map((r) => toRow(r, user, now));
  const next = res.rows.length > limit ? (rows[rows.length - 1]?.id ?? null) : null;

  // What the other tabs would hold, so the board can label them without asking
  // three more times.
  const tally = await sql.query<{ activity: Activity; n: string }>(
    `select t.activity, count(*)::text as n from (${APPLICATION_SELECT}) t group by t.activity`,
    [user.id],
  );
  const counts = { active: 0, stale: 0, closed: 0, open: 0, all: 0 };
  for (const row of tally.rows) {
    const n = Number(row.n);
    counts[row.activity] = n;
    counts.all += n;
    if (row.activity !== 'closed') counts.open += n;
  }

  return { rows, next_cursor: next, counts };
}

export async function getApplication(
  sql: pg.PoolClient,
  user: UserContext,
  id: string,
): Promise<(ApplicationRow & { events: unknown[]; facts: Record<string, unknown> }) | null> {
  const res = await sql.query<RawApplicationRow>(`${APPLICATION_SELECT} and a.id = $2`, [user.id, id]);
  const raw = res.rows[0];
  if (!raw) return null;
  const row = toRow(raw, user, new Date());

  const events = await sql.query<{
    id: string;
    type: string;
    occurred_at: Date;
    to_stage: string | null;
    payload: Record<string, unknown>;
    confidence: string;
    rung: number | null;
    evidence_ref: string | null;
  }>(
    `select id, type, occurred_at, to_stage, payload, confidence, rung, evidence_ref
       from application_events where application_id = $1 order by occurred_at desc, id desc`,
    [id],
  );

  const extra = await sql.query<{ ats_vendor: string | null; posting_url: string | null }>(
    `select ats_vendor, posting_url from sources where application_id = $1 order by first_seen_at limit 1`,
    [id],
  );
  const comp = await sql.query<{ min_minor: string; max_minor: string | null; currency: string; kind: string }>(
    `select min_minor, max_minor, currency, kind from comp_offers where application_id = $1`,
    [id],
  );
  const detail = await sql.query<{ location: string | null; work_mode: string | null }>(
    `select location, work_mode from applications where id = $1`,
    [id],
  );

  return {
    ...row,
    facts: {
      applied: row.applied_at,
      ats: extra.rows[0]?.ats_vendor ?? null,
      posting_url: extra.rows[0]?.posting_url ?? null,
      location: [detail.rows[0]?.location, detail.rows[0]?.work_mode].filter(Boolean).join(' · ') || null,
      posted_range: comp.rows.find((c) => c.kind === 'posted_range') ?? null,
      offers: comp.rows.filter((c) => c.kind === 'offer'),
    },
    events: events.rows.map((e) => ({
      id: e.id,
      when: e.occurred_at.toISOString(),
      what: eventTitle(e.type, e.to_stage, user.stages),
      detail: eventDetail(e.payload),
      // Provenance is shown on every automatically-derived claim; it is what
      // makes the automation trustworthy.
      source: provenance(e.rung, e.payload),
      conf: Number(e.confidence).toFixed(2),
      rung: e.rung,
      evidence_ref: e.evidence_ref,
    })),
  };
}

function eventTitle(type: string, toStage: string | null, stages: StageTable): string {
  switch (type) {
    case 'applied': return 'Applied';
    case 'acknowledged': return 'Acknowledged';
    case 'stage_advanced': return toStage ? stages.labelOf(toStage) : 'Stage changed';
    case 'interview_scheduled': return 'Interview scheduled';
    case 'interview_held': return 'Interview held';
    case 'deadline_set': return 'Deadline detected';
    case 'offer_received': return 'Offer received';
    case 'offer_negotiated': return 'Offer revised';
    case 'rejected': return 'Rejected';
    case 'withdrawn': return 'Withdrawn';
    case 'accepted': return 'Accepted';
    case 'went_silent': return 'Went quiet';
    case 'human_corrected': return 'You corrected this';
    case 'note_added': return 'Note';
    default: return type;
  }
}

function eventDetail(payload: Record<string, unknown>): string {
  if (typeof payload.note === 'string') return payload.note;
  if (typeof payload.text === 'string') return payload.text;
  if (payload.ats_vendor) return `Automated reply from ${String(payload.ats_vendor)}`;
  if (payload.starts_at) return `Invite for ${String(payload.starts_at).slice(0, 16).replace('T', ' ')}`;
  if (payload.due_at) return `Due ${String(payload.due_at).slice(0, 16).replace('T', ' ')}`;
  if (payload.field) return `${String(payload.field)}: ${String(payload.from)} → ${String(payload.to)}`;
  if (payload.days_quiet) return `No inbound signal for ${String(payload.days_quiet)} days`;
  return '';
}

/** "gmail · template", "calendar · ics", "gmail · model", "quick add". */
function provenance(rung: number | null, payload: Record<string, unknown>): string {
  if (rung === 4 || rung === null) return 'quick add';
  if (rung === 1) return payload.ats_vendor ? `gmail · ${String(payload.ats_vendor)}` : 'gmail · template';
  if (rung === 2) return payload.calendar_event_id ? 'calendar · ics' : 'gmail · thread';
  return 'gmail · model';
}

// ── Today ──────────────────────────────────────────────────────────────────

export async function buildToday(sql: pg.PoolClient, user: UserContext) {
  const now = new Date();

  // Counted over `activity`, not over `status`. "Live" used to mean "a row we
  // have never had a reason to close", which on a twelve-month mailbox is most
  // of them; it now means what the word means on the screen it appears on.
  const counters = await sql.query<{
    live: string;
    quiet: string;
    interviewing: string;
    offer: string;
    overdue: string;
    closed: string;
  }>(
    `select
       count(*) filter (where t.activity = 'active')::text as live,
       count(*) filter (where t.activity = 'stale')::text as quiet,
       count(*) filter (where t.activity <> 'closed' and t.current_phase = 'interviewing')::text as interviewing,
       count(*) filter (where t.activity <> 'closed' and t.current_stage in ('offer','negotiating'))::text as offer,
       count(*) filter (where t.activity = 'stale')::text as overdue,
       count(*) filter (where t.activity = 'closed')::text as closed
     from (${APPLICATION_SELECT}) t`,
    [user.id],
  );

  const weekEvents = await sql.query<{
    application_id: string;
    type: string;
    occurred_at: Date;
    from_stage: string | null;
    to_stage: string | null;
    confidence: string;
  }>(
    `select application_id, type, occurred_at, from_stage, to_stage, confidence
       from application_events
      where user_id = $1 and occurred_at > now() - interval '7 days'`,
    [user.id],
  );

  const suggestions = await sql.query<{ key: string; rule: string; payload: Record<string, unknown> }>(
    `select key, rule, payload from suggestions
      where user_id = $1 and acted_at is null and dismissed_at is null
        and (snoozed_until is null or snoozed_until < now())
        and (expires_at is null or expires_at > now())
      order by created_at desc limit 3`,
    [user.id],
  );

  const nextInterview = await sql.query<{
    id: string;
    application_id: string;
    company: string;
    role_title: string;
    stage: string;
    starts_at: Date;
    rounds: string;
  }>(
    `select i.id, i.application_id, c.canonical_name as company, a.role_title, i.stage, i.starts_at,
            (select count(*)::text from interviews x
              where x.application_id = i.application_id and x.held) as rounds
       from interviews i
       join applications a on a.id = i.application_id
       join companies c on c.id = a.company_id
      where i.user_id = $1 and i.cancelled_at is null and i.starts_at > now()
      order by i.starts_at limit 1`,
    [user.id],
  );

  const recent = await sql.query<{
    application_id: string;
    company: string;
    type: string;
    to_stage: string | null;
    occurred_at: Date;
    status: AppStatus;
    payload: Record<string, unknown>;
  }>(
    `select e.application_id, c.canonical_name as company, e.type, e.to_stage, e.occurred_at,
            a.status, e.payload
       from application_events e
       join applications a on a.id = e.application_id
       join companies c on c.id = a.company_id
      where e.user_id = $1 and e.occurred_at > now() - interval '7 days'
        and e.type <> 'went_silent'
      order by e.occurred_at desc limit 8`,
    [user.id],
  );

  const health = await mailboxHealth(sql, user.id);
  const reviewCount = await sql.query<{ n: string }>(
    `select count(*)::text as n from review_items where user_id = $1 and resolved_at is null`,
    [user.id],
  );

  const domainEvents: DomainEvent[] = weekEvents.rows.map((e) => ({
    type: e.type as DomainEvent['type'],
    occurred_at: e.occurred_at,
    from_stage: e.from_stage,
    to_stage: e.to_stage,
    confidence: Number(e.confidence),
    evidence_ref: e.application_id,
  }));

  const headline = buildHeadline({
    events: domainEvents,
    applicationIdOf: (ev) => String(ev.evidence_ref),
    liveCount: Number(counters.rows[0]?.live ?? '0'),
    openSuggestionCount: suggestions.rowCount ?? 0,
    now,
    stages: user.stages,
  });

  return {
    eyebrow: dateEyebrow(now, user.tz, user.locale),
    headline: headline.lines,
    headline_kind: headline.kind,
    counters: {
      live: Number(counters.rows[0]?.live ?? '0'),
      quiet: Number(counters.rows[0]?.quiet ?? '0'),
      interviewing: Number(counters.rows[0]?.interviewing ?? '0'),
      offer: Number(counters.rows[0]?.offer ?? '0'),
      overdue: Number(counters.rows[0]?.overdue ?? '0'),
      closed: Number(counters.rows[0]?.closed ?? '0'),
    },
    review_count: Number(reviewCount.rows[0]?.n ?? '0'),
    next_interview: nextInterview.rows[0]
      ? {
          application_id: nextInterview.rows[0].application_id,
          company: nextInterview.rows[0].company,
          role: nextInterview.rows[0].role_title,
          stage: user.stages.labelOf(nextInterview.rows[0].stage),
          starts_at: nextInterview.rows[0].starts_at.toISOString(),
          rounds_done: Number(nextInterview.rows[0].rounds),
          provenance: 'from calendar invite',
        }
      : null,
    suggestions: suggestions.rows.map((s) => ({ key: s.key, rule: s.rule, ...s.payload })),
    recent_events: recent.rows.map((e) => ({
      application_id: e.application_id,
      company: e.company,
      what: recentLabel(e.type, e.to_stage, user.stages, e.payload),
      when: new Intl.DateTimeFormat(user.locale, { weekday: 'short', timeZone: user.tz }).format(e.occurred_at),
      closed: e.status !== 'live',
    })),
    mailbox_health: health,
    closing_line:
      'Everything above was read from your mailbox and calendar. You have not typed anything this week.',
  };
}

function recentLabel(
  type: string,
  toStage: string | null,
  stages: StageTable,
  payload: Record<string, unknown>,
): string {
  switch (type) {
    case 'offer_received':
      return payload.min_minor
        ? `Offer received · ${formatMoney(Number(payload.min_minor), String(payload.currency ?? 'EUR'))}`
        : 'Offer received';
    case 'rejected':
      return payload.after_stage ? `Rejected after the ${String(payload.after_stage)}` : 'Rejected';
    case 'interview_scheduled':
      return `${toStage ? stages.labelOf(toStage) : 'Interview'} scheduled`;
    case 'stage_advanced':
      return toStage ? `Moved to ${stages.labelOf(toStage).toLowerCase()}` : 'Stage changed';
    case 'acknowledged':
      return 'Application acknowledged';
    case 'applied':
      return 'Application sent';
    default:
      return type.replace(/_/g, ' ');
  }
}

/** "Mar 26" from "2026-03". Written here so the axis is not built in a browser. */
function monthLabel(month: string): string {
  const [year, m] = month.split('-');
  const date = new Date(Date.UTC(Number(year), Number(m) - 1, 1));
  return `${new Intl.DateTimeFormat('en-GB', { month: 'short', timeZone: 'UTC' }).format(date)} ${year!.slice(2)}`;
}

export function formatMoney(minor: number, currency: string): string {
  return new Intl.NumberFormat('en-GB', {
    style: 'currency',
    currency,
    maximumFractionDigits: 0,
  }).format(minor / 100);
}

/**
 * "Is it still reading my mail?" — the one question that matters. A silent
 * connector is indistinguishable from a quiet job market, so freshness is
 * always on screen.
 */
export async function mailboxHealth(sql: pg.PoolClient, userId: string) {
  const res = await sql.query<{
    provider: string;
    status: string;
    last_ok_at: Date | null;
    backlog_estimate: number;
  }>(
    `select provider, status, last_ok_at, backlog_estimate from mailbox_accounts where user_id = $1`,
    [userId],
  );
  const placedToday = await sql.query<{ n: string }>(
    `select count(*)::text as n from seen_messages
      where user_id = $1 and outcome = 'placed' and processed_at > date_trunc('day', now())`,
    [userId],
  );

  const worst = res.rows.reduce<Date | null>(
    (acc, r) => (!acc || (r.last_ok_at && r.last_ok_at < acc) ? (r.last_ok_at ?? acc) : acc),
    null,
  );
  const backlog = res.rows.reduce((a, r) => a + r.backlog_estimate, 0);
  const needsReauth = res.rows.some((r) => r.status === 'needs_reauth');

  return {
    connected: (res.rowCount ?? 0) > 0 && !needsReauth,
    providers: res.rows.map((r) => ({ provider: r.provider, status: r.status, last_ok_at: r.last_ok_at })),
    last_ok_at: worst?.toISOString() ?? null,
    minutes_since_read: worst ? Math.floor((Date.now() - worst.getTime()) / 60_000) : null,
    placed_today: Number(placedToday.rows[0]?.n ?? '0'),
    backlog,
    // F1 needs a full screen; F2 is a strip; F4 is a per-component list.
    state: needsReauth ? 'F1' : backlog > 0 ? 'F2' : 'ok',
  };
}

// ── Statistics ─────────────────────────────────────────────────────────────

export type Period = '90d' | '12m' | 'all';

/**
 * The period window.
 *
 * `coalesce(applied_at, created_at)`, not `applied_at` alone: an application
 * whose only signal was an ATS acknowledgement has no applied date — we know it
 * was sent, we just never saw the confirmation. Filtering on `applied_at` drops
 * those rows out of the *denominator*, which is the one direction a funnel is
 * allowed to lie in. Falling back to when we learned of it is the honest
 * approximation, and it is visibly wrong by at most a day or two.
 */
const PERIOD_SQL: Record<Period, string> = {
  '90d': `and coalesce(a.applied_at, a.created_at) > now() - interval '90 days'`,
  '12m': `and coalesce(a.applied_at, a.created_at) > now() - interval '12 months'`,
  all: '',
};

/** The same window against `app_phase_reach`, which has no created_at. */
const REACH_PERIOD_SQL: Record<Period, string> = {
  '90d': `and coalesce(r.applied_at, r.created_at) > now() - interval '90 days'`,
  '12m': `and coalesce(r.applied_at, r.created_at) > now() - interval '12 months'`,
  all: '',
};

/**
 * Every statistic below is gated on how many applications are *closed*, and
 * until now "closed" meant `status <> 'live'` — a column the nightly sweep
 * writes and nothing else does. Miss a few sweeps, or read a mailbox where half
 * the applications simply stopped replying, and the gate never opens: the page
 * shows an em dash beside a funnel with twenty applications in it, which is the
 * exact complaint that started this. The cohort is judged by `activity`, so a
 * process that has been silent for three months counts as the closed
 * application it is.
 */
const ACTIVITY_CTE = `act as (
  select a.id, a.status, a.current_stage, a.current_phase, ${ACTIVITY_SQL} as activity
    from applications a
    left join stage_defs sd on sd.user_id = a.user_id and sd.key = a.current_stage
    left join stage_dwell_in d on d.user_id = a.user_id and d.stage = a.current_stage and d.n >= 5
    left join lateral (
      select min(i.starts_at) as starts_at from interviews i
       where i.application_id = a.id and i.cancelled_at is null and i.starts_at > now()
    ) nx on true
   where a.user_id = $1 and a.merged_into_id is null
)`;

/** Closed without anybody ever saying no. That is what a ghost rate measures. */
const GHOSTED_SQL = `act.activity = 'closed' and act.status in ('live','dormant')`;

export async function buildStats(sql: pg.PoolClient, user: UserContext, period: Period) {
  const window = PERIOD_SQL[period];
  const reachWindow = REACH_PERIOD_SQL[period];

  const funnel = await sql.query<{ label: string; n: string }>(
    `select 'Applied' as label, count(*)::text as n from applications a
      where a.user_id = $1 and a.merged_into_id is null ${window}
     union all
     select 'Acknowledged', count(*)::text from applications a
      where a.user_id = $1 and a.merged_into_id is null ${window}
        and exists (select 1 from application_events e
                     where e.application_id = a.id and e.type = 'acknowledged')
     union all
     select 'Screening', count(*)::text from applications a
      where a.user_id = $1 and a.merged_into_id is null ${window}
        and exists (select 1 from application_events e
                      join stage_defs sd on sd.user_id = a.user_id and sd.key = e.to_stage
                     where e.application_id = a.id and sd.phase in ('screening','interviewing','decided'))
     union all
     select 'Interviewing', count(*)::text from applications a
       join app_phase_reach r on r.id = a.id
      where a.user_id = $1 and a.merged_into_id is null ${window} and r.reached_interview
     union all
     select 'Offer', count(*)::text from applications a
       join app_phase_reach r on r.id = a.id
      where a.user_id = $1 and a.merged_into_id is null ${window} and r.reached_offer`,
    [user.id],
  );

  // The maturity exclusion, made explicit: an application applied under 21 days
  // ago and still in `sent` has not had time to convert, and counting it is why
  // a naive funnel always looks like it is falling.
  const conv = await sql.query<{
    numerator: string;
    denominator: string;
    excluded: string;
    closed: string;
  }>(
    `with ${ACTIVITY_CTE}, cohort as (
       select r.*, act.activity from app_phase_reach r
       join act on act.id = r.id
       where r.user_id = $1 ${reachWindow}
     )
     select
       count(*) filter (where reached_interview and not immature)::text as numerator,
       count(*) filter (where not immature)::text as denominator,
       count(*) filter (where immature)::text as excluded,
       count(*) filter (where activity = 'closed')::text as closed
     from (select *, (activity <> 'closed' and not reached_interview
                      and applied_at > now() - interval '${RATIO_MATURITY_DAYS} days') as immature
             from cohort) t`,
    [user.id],
  );

  const offerConv = await sql.query<{ numerator: string; denominator: string; closed: string }>(
    `with ${ACTIVITY_CTE}
     select count(*) filter (where reached_offer)::text as numerator,
            count(*) filter (where reached_interview and act.activity = 'closed')::text as denominator,
            count(*) filter (where act.activity = 'closed')::text as closed
       from app_phase_reach r join act on act.id = r.id
      where r.user_id = $1 ${reachWindow}`,
    [user.id],
  );

  const timing = await sql.query<{ median_days: string | null; n: string }>(
    `select percentile_cont(0.5) within group (
              order by extract(epoch from (first_human_at - applied_at)) / 86400
            )::text as median_days,
            count(*) filter (where first_human_at is not null)::text as n
       from app_phase_reach r
      where r.user_id = $1 and applied_at is not null ${reachWindow}`,
    [user.id],
  );

  const ghost = await sql.query<{ ghosted: string; closed: string }>(
    `with ${ACTIVITY_CTE}
     select count(*) filter (where ${GHOSTED_SQL})::text as ghosted,
            count(*) filter (where act.activity = 'closed')::text as closed
       from app_phase_reach r join act on act.id = r.id
      where r.user_id = $1 ${reachWindow}`,
    [user.id],
  );

  // Inlined rather than read from `channel_effectiveness`, which counts a ghost
  // as `status = 'dormant'` and so under-reports it by exactly the applications
  // this change is about. Same grouping, same first-touch attribution.
  const channels = await sql.query<{
    channel: string;
    sent: string;
    interviews: string;
    offers: string;
    ghosted: string;
  }>(
    `with ${ACTIVITY_CTE}
     select s.channel, count(*)::text as sent,
            count(*) filter (where r.reached_interview)::text as interviews,
            count(*) filter (where r.reached_offer)::text as offers,
            count(*) filter (where ${GHOSTED_SQL})::text as ghosted
       from sources s
       join app_phase_reach r on r.id = s.application_id
       join act on act.id = s.application_id
      where s.is_first_touch and r.user_id = $1
      group by s.channel order by count(*) desc`,
    [user.id],
  );

  // Volume over time, and how much of each month came back. Three series on one
  // month axis: what you sent, what replied, what reached an interview.
  const byMonth = await sql.query<{
    month: string;
    applied: string;
    replied: string;
    interviews: string;
    offers: string;
  }>(
    `select to_char(date_trunc('month', coalesce(a.applied_at, a.created_at)), 'YYYY-MM') as month,
            count(*)::text as applied,
            count(*) filter (where r.first_human_at is not null)::text as replied,
            count(*) filter (where r.reached_interview)::text as interviews,
            count(*) filter (where r.reached_offer)::text as offers
       from applications a
       join app_phase_reach r on r.id = a.id
      where a.user_id = $1 and a.merged_into_id is null ${window}
      group by 1 order by 1`,
    [user.id],
  );

  // How they ended. Keyed on `status` inside the closed set, so the five
  // outcomes are mutually exclusive and exhaustive — the bar sums to the cohort
  // and no application is counted under two endings.
  const outcomes = await sql.query<{
    open: string;
    accepted: string;
    rejected: string;
    ghosted: string;
    withdrawn: string;
  }>(
    `with ${ACTIVITY_CTE}
     select count(*) filter (where act.activity <> 'closed')::text as open,
            count(*) filter (where act.activity = 'closed' and act.status = 'accepted')::text as accepted,
            count(*) filter (where act.activity = 'closed' and act.status = 'rejected')::text as rejected,
            count(*) filter (where ${GHOSTED_SQL})::text as ghosted,
            count(*) filter (where act.activity = 'closed' and act.status = 'withdrawn')::text as withdrawn
       from app_phase_reach r join act on act.id = r.id
      where r.user_id = $1 ${reachWindow}`,
    [user.id],
  );

  const dwell = await sql.query<{ stage: string; p50_days: string; n: string }>(
    `select stage, p50_days::text, n::text from stage_dwell_in where user_id = $1`,
    [user.id],
  );

  const comp = await sql.query<{ kind: string; min_minor: string; max_minor: string | null; currency: string }>(
    `select kind, min_minor::text, max_minor::text, currency from comp_offers where user_id = $1`,
    [user.id],
  );

  const quarters = await sql.query<{ n: string }>(
    `select count(distinct date_trunc('quarter', applied_at))::text as n
       from applications where user_id = $1 and applied_at is not null`,
    [user.id],
  );

  const closed = Number(conv.rows[0]?.closed ?? '0');

  const appToInterview: Metric = ratio({
    numerator: Number(conv.rows[0]?.numerator ?? '0'),
    denominator: Number(conv.rows[0]?.denominator ?? '0'),
    excluded: Number(conv.rows[0]?.excluded ?? '0'),
    closed,
    exclusionReason: 'too recent to count',
  });

  const interviewToOffer: Metric = ratio({
    numerator: Number(offerConv.rows[0]?.numerator ?? '0'),
    denominator: Number(offerConv.rows[0]?.denominator ?? '0'),
    closed: Number(offerConv.rows[0]?.closed ?? '0'),
  });

  const ghostRate: Metric = ratio({
    numerator: Number(ghost.rows[0]?.ghosted ?? '0'),
    denominator: Number(ghost.rows[0]?.closed ?? '0'),
    closed: Number(ghost.rows[0]?.closed ?? '0'),
  });

  const funnelRows = funnel.rows.map((f) => ({ label: f.label, n: Number(f.n) }));
  const top = funnelRows[0]?.n ?? 0;

  return {
    period,
    funnel: funnelRows.map((f) => ({ ...f, width: top ? Math.round((f.n / top) * 100) : 0 })),
    ratios: [
      { label: 'Application → interview', ...appToInterview, display: formatPercent(appToInterview.value) },
      { label: 'Interview → offer', ...interviewToOffer, display: formatPercent(interviewToOffer.value) },
    ],
    first_response: {
      value: timing.rows[0]?.median_days ? Number(timing.rows[0].median_days) : null,
      n: Number(timing.rows[0]?.n ?? '0'),
      display: timing.rows[0]?.median_days ? formatDays(Number(timing.rows[0].median_days)) : '—',
      caption: 'median to first human reply',
    },
    ghost: { ...ghostRate, display: formatPercent(ghostRate.value), caption: 'closed by silence, not by a no' },
    channels: channels.rows.map((c) => {
      const sent = Number(c.sent);
      const gate = sent >= GATES.CHANNEL_MIN_APPLICATIONS;
      return {
        name: c.channel,
        sent,
        gate_met: gate,
        iv: gate ? formatPercent(Number(c.interviews) / sent) : '—',
        of: gate ? formatPercent(Number(c.offers) / sent) : '—',
        ghost: gate ? formatPercent(Number(c.ghosted) / sent) : '—',
        // The raw rates as well as the strings: a bar cannot be drawn from an
        // em dash, and the client is still forbidden from doing the division.
        iv_value: gate ? Number(c.interviews) / sent : null,
        of_value: gate ? Number(c.offers) / sent : null,
        ghost_value: gate ? Number(c.ghosted) / sent : null,
        interviews: Number(c.interviews),
        offers: Number(c.offers),
        ghosted: Number(c.ghosted),
        note: gate ? '' : `${sent} of ${GATES.CHANNEL_MIN_APPLICATIONS} needed`,
      };
    }),
    // Referrals are always their own row, never folded into a board.
    channel_note:
      'Referrals are reported separately on purpose — folding them into LinkedIn would flatter it.',
    time_in_stage: dwell.rows.map((d) => ({
      stage: user.stages.labelOf(d.stage),
      // Rounded here, and shipped with the string to print. `p50_days` is a
      // percentile over epoch seconds, so it arrives as 12.416666666666666 and
      // the client used to render every digit of it.
      days: Math.round(Number(d.p50_days) * 10) / 10,
      display: formatDays(Number(d.p50_days)),
      n: Number(d.n),
      gate_met: Number(d.n) >= GATES.TIME_IN_STAGE_MIN_TRANSITIONS,
    })),
    by_month: byMonth.rows.map((m) => ({
      month: m.month,
      label: monthLabel(m.month),
      applied: Number(m.applied),
      replied: Number(m.replied),
      interviews: Number(m.interviews),
      offers: Number(m.offers),
    })),
    outcomes: {
      open: Number(outcomes.rows[0]?.open ?? '0'),
      accepted: Number(outcomes.rows[0]?.accepted ?? '0'),
      rejected: Number(outcomes.rows[0]?.rejected ?? '0'),
      ghosted: Number(outcomes.rows[0]?.ghosted ?? '0'),
      withdrawn: Number(outcomes.rows[0]?.withdrawn ?? '0'),
    },
    compensation: buildCompSpread(comp.rows, user.displayCurrency),
    seasonal: {
      gate_met: Number(quarters.rows[0]?.n ?? '0') >= GATES.SEASONAL_MIN_QUARTERS,
      note: 'Seasonal averages need two quarters of history before they mean anything, so they are hidden rather than shown noisy.',
    },
  };
}

/**
 * The compensation figure. The prototype hard-codes percentages; this computes
 * a real min/max domain so the three tracks share one implicit scale.
 */
function buildCompSpread(
  rows: Array<{ kind: string; min_minor: string; max_minor: string | null; currency: string }>,
  displayCurrency: string,
) {
  const usable = rows.filter((r) => r.currency === displayCurrency);
  const dropped = rows.length - usable.length;
  const values = usable.flatMap((r) => [Number(r.min_minor), r.max_minor ? Number(r.max_minor) : null])
    .filter((v): v is number => v !== null);

  if (values.length === 0) {
    return { domain: null, posted: [], ask: null, offers: [], currency: displayCurrency, dropped };
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pct = (v: number): number => Math.round(((v - min) / span) * 100);

  return {
    domain: { min, max, currency: displayCurrency },
    posted: usable
      .filter((r) => r.kind === 'posted_range')
      .map((r) => ({
        from: pct(Number(r.min_minor)),
        to: pct(r.max_minor ? Number(r.max_minor) : Number(r.min_minor)),
        label: `${formatMoney(Number(r.min_minor), r.currency)}–${formatMoney(Number(r.max_minor ?? r.min_minor), r.currency)}`,
      })),
    ask: usable.find((r) => r.kind === 'ask')
      ? { at: pct(Number(usable.find((r) => r.kind === 'ask')!.min_minor)) }
      : null,
    offers: usable
      .filter((r) => r.kind === 'offer')
      .map((r) => ({ at: pct(Number(r.min_minor)), label: formatMoney(Number(r.min_minor), r.currency) })),
    currency: displayCurrency,
    // Currencies we cannot convert are listed, never folded in. decisions.md C5.
    dropped,
  };
}
