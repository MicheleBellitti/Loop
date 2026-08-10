import { all, type Sql } from './client.js';
import { projectApplication } from './events.js';

/**
 * "applications can be dropped and rebuilt from application_events by one
 * function; a test asserts the rebuild is byte-identical" (Spec §04).
 *
 * This is that function. It blanks every derived column and re-folds, so a
 * rebuild that produced the same row by accident — because the old value was
 * still sitting there — would not pass.
 */

/** Columns that are *not* derived, and therefore survive a rebuild. */
export const NON_DERIVED_COLUMNS = ['id', 'user_id', 'created_at', 'manually_created'] as const;

export async function resetProjection(sql: Sql, applicationId: string): Promise<void> {
  await sql.query(
    `update applications set
       current_stage = 'applied',
       current_phase = 'sent',
       status = 'live',
       applied_at = null,
       last_signal_at = null,
       went_dormant_at = null,
       last_user_action_at = null,
       awaiting_them = true,
       seniority = null,
       location = null,
       work_mode = null,
       comp_expectation_minor = null,
       comp_currency = null,
       confidence = 1.0,
       needs_review = false
     where id = $1`,
    [applicationId],
  );
}

export async function rebuildApplication(
  sql: Sql,
  userId: string,
  applicationId: string,
): Promise<void> {
  await resetProjection(sql, applicationId);
  await projectApplication(sql, userId, applicationId);
}

export async function rebuildAll(sql: Sql, userId: string): Promise<number> {
  const rows = await all<{ id: string }>(
    sql,
    `select id from applications where user_id = $1 order by id`,
    [userId],
  );
  for (const r of rows) await rebuildApplication(sql, userId, r.id);
  await sql.query('select refresh_projections()');
  return rows.length;
}

/**
 * The comparable shape a rebuild test snapshots.
 *
 * `only` narrows it to specific applications. The invariant being tested is
 * "the projection the pipeline maintained incrementally equals the projection
 * rebuilt from the log alone", so the comparison has to be scoped to rows that
 * were actually maintained — a row nobody ever projected differs from its
 * rebuild for a reason that has nothing to do with the fold.
 */
export async function snapshotApplications(
  sql: Sql,
  userId: string,
  only?: readonly string[],
): Promise<unknown[]> {
  return all(
    sql,
    `select id, company_id, role_title, seniority, location, work_mode,
            current_stage, current_phase, status, applied_at, last_signal_at,
            went_dormant_at, last_user_action_at, awaiting_them,
            comp_expectation_minor, comp_currency, confidence, needs_review
       from applications
      where user_id = $1
        and ($2::uuid[] is null or id = any($2))
      order by id`,
    [userId, only ?? null],
  );
}
