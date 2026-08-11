import type { DomainEvent, EventPayload, EventType, Rung } from '@loop/domain';
import { fold, StageTable, type StageDef } from '@loop/domain';
import { all, exactlyOne, one, type Sql } from './client.js';

/**
 * Appending to the log and folding it back into the row.
 *
 * Only the pipeline service holds the grants that let these statements run —
 * the database enforces the single-writer rule, so this module being imported
 * elsewhere fails loudly rather than quietly corrupting state.
 */

export interface EventRow {
  id: string;
  application_id: string;
  user_id: string;
  type: EventType;
  occurred_at: Date;
  recorded_at: Date;
  from_stage: string | null;
  to_stage: string | null;
  payload: EventPayload;
  source_id: string | null;
  confidence: string | number;
  evidence_ref: string | null;
  rung: number | null;
}

export function toDomainEvent(row: EventRow): DomainEvent {
  return {
    id: row.id,
    type: row.type,
    occurred_at: new Date(row.occurred_at),
    recorded_at: new Date(row.recorded_at),
    from_stage: row.from_stage,
    to_stage: row.to_stage,
    payload: row.payload ?? {},
    confidence: Number(row.confidence),
    evidence_ref: row.evidence_ref,
    rung: (row.rung ?? null) as Rung | null,
  };
}

export interface AppendInput {
  userId: string;
  applicationId: string;
  type: EventType;
  occurredAt: Date;
  confidence: number;
  fromStage?: string | null;
  toStage?: string | null;
  payload?: EventPayload;
  sourceId?: string | null;
  evidenceRef?: string | null;
  rung?: Rung | null;
}

/**
 * Idempotent by construction: the unique index on
 * (application_id, type, occurred_at, evidence_ref) with NULLS NOT DISTINCT
 * means delivering the same queue message twice produces one row. Returns null
 * when the event was already there, which is how the caller knows not to
 * re-notify.
 */
export async function appendEvent(sql: Sql, input: AppendInput): Promise<string | null> {
  const row = await one<{ id: string }>(
    sql,
    `insert into application_events
       (application_id, user_id, type, occurred_at, from_stage, to_stage,
        payload, source_id, confidence, evidence_ref, rung)
     values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
     on conflict do nothing
     returning id`,
    [
      input.applicationId,
      input.userId,
      input.type,
      input.occurredAt,
      input.fromStage ?? null,
      input.toStage ?? null,
      JSON.stringify(input.payload ?? {}),
      input.sourceId ?? null,
      input.confidence,
      input.evidenceRef ?? null,
      input.rung ?? null,
    ],
  );
  return row?.id ?? null;
}

export async function loadEvents(sql: Sql, applicationId: string): Promise<DomainEvent[]> {
  const rows = await all<EventRow>(
    sql,
    `select * from application_events where application_id = $1 order by occurred_at, id`,
    [applicationId],
  );
  return rows.map(toDomainEvent);
}

export async function loadStageTable(sql: Sql, userId: string): Promise<StageTable> {
  const rows = await all<StageDef & { stale_after_days: number; depth: number }>(
    sql,
    `select key, label, phase, depth, stale_after_days from stage_defs where user_id = $1 order by depth`,
    [userId],
  );
  return new StageTable(rows.length ? rows : undefined);
}

/**
 * Stages where the ball is in the user's court.
 *
 * The follow-up rule needs "awaiting them", which the spec names but never
 * derives. A take-home is waiting on you by definition; so is an offer you have
 * not answered. Everything else is waiting on them.
 */
const USER_OWES_A_MOVE = new Set(['take_home', 'offer', 'negotiating']);

const HUMAN_AUTHORED = new Set<EventType>([
  'human_corrected',
  'note_added',
  'withdrawn',
  'accepted',
]);

/**
 * Recompute the projection for one application from its log alone.
 *
 * Every column it writes is derived; drop the row, run this, and you have the
 * same row back. That property is asserted by a test, and it is the reason the
 * extractor can be improved next month and re-derive last month's history.
 */
export async function projectApplication(
  sql: Sql,
  userId: string,
  applicationId: string,
): Promise<void> {
  const [events, stages] = await Promise.all([
    loadEvents(sql, applicationId),
    loadStageTable(sql, userId),
  ]);
  if (events.length === 0) return;

  const state = fold(events, { stages });

  let wentDormantAt: Date | null = null;
  let lastUserActionAt: Date | null = null;
  let presumedClosed = false;
  for (const ev of events) {
    if (ev.type === 'went_silent') {
      wentDormantAt = ev.occurred_at;
      presumedClosed = ev.payload?.presumed_closed === true;
    }
    if (HUMAN_AUTHORED.has(ev.type) || ev.rung === 4) {
      if (!lastUserActionAt || ev.occurred_at > lastUserActionAt) lastUserActionAt = ev.occurred_at;
    }
  }
  if (state.status !== 'dormant') {
    wentDormantAt = null;
    presumedClosed = false;
  }

  const openDeadline = await one<{ n: string }>(
    sql,
    `select count(*)::text as n from deadlines
      where application_id = $1 and met_at is null and due_at > now()`,
    [applicationId],
  );
  const needsReview = await one<{ n: string }>(
    sql,
    `select count(*)::text as n from review_items
      where application_id = $1 and resolved_at is null`,
    [applicationId],
  );

  const awaitingThem =
    state.status === 'live' &&
    !USER_OWES_A_MOVE.has(state.current_stage) &&
    Number(openDeadline?.n ?? '0') === 0;

  await sql.query(
    `update applications set
       current_stage = $2,
       current_phase = $3,
       status = $4,
       applied_at = $5,
       last_signal_at = $6,
       went_dormant_at = $7,
       last_user_action_at = $8,
       awaiting_them = $9,
       presumed_closed = $18,
       role_title = coalesce($10, role_title),
       seniority = coalesce($11, seniority),
       location = coalesce($12, location),
       work_mode = coalesce($13, work_mode),
       comp_expectation_minor = $14,
       comp_currency = $15,
       confidence = $16,
       needs_review = $17
     where id = $1`,
    [
      applicationId,
      state.current_stage,
      state.current_phase,
      state.status,
      state.applied_at,
      state.last_signal_at,
      wentDormantAt,
      lastUserActionAt,
      awaitingThem,
      state.role_title,
      state.seniority,
      state.location,
      state.work_mode,
      state.comp_expectation_minor,
      state.comp_currency,
      state.confidence,
      Number(needsReview?.n ?? '0') > 0,
      presumedClosed,
    ],
  );
}

/** Satellite rows that some events imply. Written by the pipeline only. */
export async function applyEventSideEffects(
  sql: Sql,
  userId: string,
  applicationId: string,
  eventId: string,
  ev: DomainEvent,
): Promise<void> {
  switch (ev.type) {
    case 'interview_scheduled': {
      const p = ev.payload ?? {};
      if (!p.starts_at) return;
      await sql.query(
        `insert into interviews (user_id, application_id, stage, starts_at, ends_at, location, calendar_event_id)
         values ($1,$2,$3,$4,$5,$6,$7)
         on conflict (user_id, calendar_event_id) do update
           set starts_at = excluded.starts_at,
               ends_at = excluded.ends_at,
               stage = excluded.stage,
               cancelled_at = null`,
        [
          userId,
          applicationId,
          p.stage ?? 'technical',
          p.starts_at,
          p.ends_at ?? null,
          p.location ?? null,
          p.calendar_event_id ?? null,
        ],
      );
      return;
    }
    case 'interview_held': {
      if (!ev.payload?.interview_id) return;
      await sql.query(`update interviews set held = true where id = $1 and user_id = $2`, [
        ev.payload.interview_id,
        userId,
      ]);
      return;
    }
    case 'deadline_set': {
      const p = ev.payload ?? {};
      if (!p.due_at) return;
      await sql.query(
        `insert into deadlines (user_id, application_id, kind, due_at, url, source, source_event_id)
         values ($1,$2,$3,$4,$5,$6,$7)
         on conflict do nothing`,
        [userId, applicationId, p.kind ?? 'take_home', p.due_at, p.url ?? null, p.source ?? 'gmail', eventId],
      );
      return;
    }
    case 'offer_received':
    case 'offer_negotiated': {
      const p = ev.payload ?? {};
      if (p.min_minor === undefined || !p.currency) return;
      await sql.query(
        `insert into comp_offers (user_id, application_id, kind, min_minor, max_minor, currency, equity_note, decide_by, source_event_id)
         values ($1,$2,'offer',$3,$4,$5,$6,$7,$8)`,
        [
          userId,
          applicationId,
          p.min_minor,
          p.max_minor ?? null,
          p.currency.toUpperCase(),
          p.equity_note ?? null,
          p.decide_by ?? null,
          eventId,
        ],
      );
      return;
    }
    default:
      return;
  }
}

export async function applicationExists(sql: Sql, id: string): Promise<boolean> {
  const row = await one<{ ok: boolean }>(sql, `select true as ok from applications where id = $1`, [id]);
  return !!row;
}

export async function listApplicationIds(sql: Sql, userId: string): Promise<string[]> {
  const rows = await all<{ id: string }>(sql, `select id from applications where user_id = $1`, [userId]);
  return rows.map((r) => r.id);
}

export async function countEvents(sql: Sql, userId: string): Promise<number> {
  const row = await exactlyOne<{ n: string }>(
    sql,
    `select count(*)::text as n from application_events where user_id = $1`,
    [userId],
  );
  return Number(row.n);
}
