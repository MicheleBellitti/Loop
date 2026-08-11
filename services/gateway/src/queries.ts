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
  StageTable,
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
  needs_review: boolean;
  confidence: number;
}

const APPLICATION_SELECT = `
  select a.id, c.canonical_name as company, a.role_title, a.current_stage, a.current_phase,
         a.status, a.applied_at, a.last_signal_at, a.needs_review, a.presumed_closed, a.confidence,
         s.channel,
         sd.stale_after_days,
         d.p90_days,
         (select min(due_at) from deadlines dl
           where dl.application_id = a.id and dl.met_at is null and dl.due_at > now()) as deadline_at,
         (select min(decide_by) from comp_offers co
           where co.application_id = a.id and co.decide_by is not null) as decide_by
    from applications a
    join companies c on c.id = a.company_id
    left join sources s on s.application_id = a.id and s.is_first_touch
    left join stage_defs sd on sd.user_id = a.user_id and sd.key = a.current_stage
    left join stage_dwell_in d on d.user_id = a.user_id and d.stage = a.current_stage and d.n >= 5
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
    closed: r.status !== 'live',
    needs_review: r.needs_review,
    confidence: Number(r.confidence),
  };
}

export interface ListOptions {
  phase?: string;
  status?: string;
  sort?: 'last_signal' | 'stage_depth' | 'company';
  cursor?: string;
  limit?: number;
}

export async function listApplications(
  sql: pg.PoolClient,
  user: UserContext,
  opts: ListOptions,
): Promise<{ rows: ApplicationRow[]; next_cursor: string | null }> {
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

  const order =
    opts.sort === 'company'
      ? 'c.canonical_name asc, a.id asc'
      : opts.sort === 'stage_depth'
        ? 'coalesce(sd.depth, 0) desc, a.id asc'
        : 'a.last_signal_at desc nulls last, a.id asc';

  const limit = Math.min(opts.limit ?? 100, 200);
  params.push(limit + 1);

  const res = await sql.query<RawApplicationRow>(
    `${APPLICATION_SELECT}${where} order by ${order} limit $${params.length}`,
    params,
  );

  const now = new Date();
  const rows = res.rows.slice(0, limit).map((r) => toRow(r, user, now));
  const next = res.rows.length > limit ? (rows[rows.length - 1]?.id ?? null) : null;
  return { rows, next_cursor: next };
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

  const counters = await sql.query<{ live: string; interviewing: string; offer: string; overdue: string }>(
    `select
       count(*) filter (where status = 'live')::text as live,
       count(*) filter (where status = 'live' and current_phase = 'interviewing')::text as interviewing,
       count(*) filter (where current_stage in ('offer','negotiating') and status = 'live')::text as offer,
       count(*) filter (where status = 'live' and last_signal_at < now() - interval '21 days')::text as overdue
     from applications where user_id = $1 and merged_into_id is null`,
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
      interviewing: Number(counters.rows[0]?.interviewing ?? '0'),
      offer: Number(counters.rows[0]?.offer ?? '0'),
      overdue: Number(counters.rows[0]?.overdue ?? '0'),
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
    `with cohort as (
       select r.*, a.current_phase from app_phase_reach r
       join applications a on a.id = r.id
       where r.user_id = $1 ${reachWindow}
     )
     select
       count(*) filter (where reached_interview and not immature)::text as numerator,
       count(*) filter (where not immature)::text as denominator,
       count(*) filter (where immature)::text as excluded,
       count(*) filter (where status <> 'live')::text as closed
     from (select *, (status = 'live' and not reached_interview
                      and applied_at > now() - interval '21 days') as immature
             from cohort) t`,
    [user.id],
  );

  const offerConv = await sql.query<{ numerator: string; denominator: string; closed: string }>(
    `select count(*) filter (where reached_offer)::text as numerator,
            count(*) filter (where reached_interview and status <> 'live')::text as denominator,
            count(*) filter (where status <> 'live')::text as closed
       from app_phase_reach r
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
    `select count(*) filter (where status = 'dormant')::text as ghosted,
            count(*) filter (where status <> 'live')::text as closed
       from app_phase_reach r
      where r.user_id = $1 ${reachWindow}`,
    [user.id],
  );

  const channels = await sql.query<{
    channel: string;
    sent: string;
    interviews: string;
    offers: string;
    ghosted: string;
  }>(
    `select channel, sent::text, interviews::text, offers::text, ghosted::text
       from channel_effectiveness where user_id = $1 order by sent desc`,
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
        note: gate ? '' : `${sent} of ${GATES.CHANNEL_MIN_APPLICATIONS} needed`,
      };
    }),
    // Referrals are always their own row, never folded into a board.
    channel_note:
      'Referrals are reported separately on purpose — folding them into LinkedIn would flatter it.',
    time_in_stage: dwell.rows.map((d) => ({
      stage: user.stages.labelOf(d.stage),
      days: Number(d.p50_days),
      n: Number(d.n),
      gate_met: Number(d.n) >= GATES.TIME_IN_STAGE_MIN_TRANSITIONS,
    })),
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
