import type pg from 'pg';
import { publish, QUEUES } from '@loop/queue';
import { M, type Logger } from '@loop/runtime';
import { CONNECTOR, domainOfAddress, type RawMessage } from '@loop/domain';
import { toRawMessage } from './normalise.js';
import {
  GoogleAuthError,
  HistoryTooOld,
  markError,
  markNeedsReauth,
  markOk,
  readRefreshToken,
  saveCursor,
  setBacklog,
  type CalendarEvent,
  type GoogleClient,
  type MailboxRow,
} from '@loop/google';

/**
 * The read loop.
 *
 * Everything downstream of here is replayable from `seen_messages`, and the
 * cursor only advances after a batch is published — so the box can be down for
 * a day and lose nothing but timeliness.
 */

export interface SyncDeps {
  pool: pg.Pool;
  google: GoogleClient;
  log: Logger;
}

async function accessTokenFor(deps: SyncDeps, mailbox: MailboxRow): Promise<string> {
  const refresh = await readRefreshToken(mailbox);
  const tokens = await deps.google.refresh(refresh);
  return tokens.access_token;
}

/**
 * Publish one message, exactly once.
 *
 * The `seen_messages` insert and the queue publish share a transaction: a
 * message is either recorded and queued, or neither. A replayed webhook is a
 * no-op because the primary key already holds the id.
 */
async function ingestMessage(
  deps: SyncDeps,
  mailbox: MailboxRow,
  messageId: string,
  opts: { backfill?: boolean } = {},
): Promise<'published' | 'skipped'> {
  const client = await deps.pool.connect();
  try {
    const seen = await client.query(
      `select 1 from seen_messages where mailbox_id = $1 and provider_message_id = $2`,
      [mailbox.id, messageId],
    );
    if (seen.rowCount) return 'skipped';

    const accessToken = await accessTokenFor(deps, mailbox);
    // A calendar part over a few kilobytes arrives as an attachment id with an
    // empty body, so the .ics has to be fetched separately or every interview
    // invite parses as null — see GoogleClient.hydrateCalendarParts.
    const gmail = await deps.google.hydrateCalendarParts(
      accessToken,
      await deps.google.getMessage(accessToken, messageId),
    );
    const raw: RawMessage = toRawMessage(gmail, {
      userId: mailbox.user_id,
      mailboxId: mailbox.id,
      backfill: opts.backfill,
    });

    await client.query('begin');
    await client.query(
      `insert into seen_messages (mailbox_id, provider_message_id, user_id, body_sha256, received_at)
       values ($1,$2,$3,decode($4,'hex'),$5)
       on conflict do nothing`,
      [mailbox.id, messageId, mailbox.user_id, raw.body_sha256, raw.received_at],
    );
    await publish(client, QUEUES.raw, raw);
    await client.query('commit');

    M.messagesRead.inc({ mailbox: mailbox.id });
    return 'published';
  } catch (err) {
    await client.query('rollback').catch(() => undefined);
    throw err;
  } finally {
    client.release();
  }
}

/** Live sync from a stored history id. */
export async function syncHistory(deps: SyncDeps, mailbox: MailboxRow): Promise<number> {
  const cursor = mailbox.cursor?.historyId;
  if (!cursor) {
    // A freshly connected mailbox has no resume point yet. Establish one and
    // stop — do NOT read history here.
    //
    // This used to run a one-month backfill as a default, which raced the user:
    // the five-minute poll fires seconds after the OAuth callback, long before
    // anyone reaches "how far back?", and by saving a cursor it made the
    // explicit choice a no-op. The window the user picks is the only thing that
    // decides how far back Loop reads, and a scan they did not ask for is not
    // a sensible default for the one operation that touches a year of mail.
    const accessToken = await accessTokenFor(deps, mailbox);
    const profile = await deps.google.profile(accessToken);
    await saveCursor(deps.pool, mailbox.id, { historyId: profile.historyId });
    deps.log.info({ mailbox_id: mailbox.id, outcome: 'cursor_established' });
    return 0;
  }

  let published = 0;
  try {
    const accessToken = await accessTokenFor(deps, mailbox);
    let pageToken: string | undefined;
    let latestHistoryId = cursor;
    const ids = new Set<string>();

    do {
      const page = await deps.google.history(accessToken, cursor, pageToken);
      for (const h of page.history ?? []) {
        for (const added of h.messagesAdded ?? []) ids.add(added.message.id);
      }
      if (page.historyId) latestHistoryId = page.historyId;
      pageToken = page.nextPageToken;
    } while (pageToken);

    for (const id of ids) {
      if ((await ingestMessage(deps, mailbox, id)) === 'published') published += 1;
    }

    // Only after the batch is published.
    await saveCursor(deps.pool, mailbox.id, { historyId: latestHistoryId });
    await markOk(deps.pool, mailbox.id);
    deps.log.info({ mailbox_id: mailbox.id, outcome: 'history_synced', count: published });
    return published;
  } catch (err) {
    if (err instanceof HistoryTooOld) {
      // "404 on historyId (older than 7 days) → full re-list of the last 30
      // days, then resume." This is failure state F2: degraded, self-healing.
      deps.log.warn({ mailbox_id: mailbox.id, outcome: 'history_expired', reason: 'relist' });
      return relist(deps, mailbox, CONNECTOR.RELIST_DAYS);
    }
    if (err instanceof GoogleAuthError && err.needsReauth) {
      await markNeedsReauth(deps.pool, mailbox.id, err.message);
      deps.log.error({ mailbox_id: mailbox.id, outcome: 'needs_reauth' });
      return 0;
    }
    await markError(deps.pool, mailbox.id, (err as Error).message);
    throw err;
  }
}

async function relist(deps: SyncDeps, mailbox: MailboxRow, days: number): Promise<number> {
  const after = new Date(Date.now() - days * 86_400_000).toISOString().slice(0, 10).replace(/-/g, '/');
  return runQuery(deps, mailbox, `after:${after}`, { backfill: false });
}

/**
 * The first scan.
 *
 * "Backfill is the same path with a date query, run at concurrency 2 and 250
 * messages per batch so a first scan cannot starve live traffic."
 */
export async function backfill(deps: SyncDeps, mailbox: MailboxRow, months: number): Promise<number> {
  const capped = Math.min(months, CONNECTOR.MAX_BACKFILL_MONTHS);
  const since = new Date();
  since.setMonth(since.getMonth() - capped);
  const after = since.toISOString().slice(0, 10).replace(/-/g, '/');

  const accessToken = await accessTokenFor(deps, mailbox);
  const profile = await deps.google.profile(accessToken);
  // Establish the resume point *before* reading, so nothing that arrives during
  // the scan is missed once the cursor is saved.
  await saveCursor(deps.pool, mailbox.id, { historyId: profile.historyId });

  return runQuery(deps, mailbox, `after:${after}`, { backfill: true });
}

async function runQuery(
  deps: SyncDeps,
  mailbox: MailboxRow,
  query: string,
  opts: { backfill: boolean },
): Promise<number> {
  let published = 0;
  let pageToken: string | undefined;
  let remaining = 0;

  do {
    const accessToken = await accessTokenFor(deps, mailbox);
    const page = await deps.google.listMessages(accessToken, query, pageToken);
    const ids = (page.messages ?? []).map((m) => m.id);
    remaining += ids.length;
    await setBacklog(deps.pool, mailbox.id, remaining - published);

    // Concurrency 2: a first scan must not starve live traffic.
    for (let i = 0; i < ids.length; i += CONNECTOR.BACKFILL_CONCURRENCY) {
      const slice = ids.slice(i, i + CONNECTOR.BACKFILL_CONCURRENCY);
      const results = await Promise.all(
        slice.map((id) =>
          ingestMessage(deps, mailbox, id, { backfill: opts.backfill }).catch((err: Error) => {
            deps.log.warn({ mailbox_id: mailbox.id, provider_message_id: id, error: err.message });
            return 'skipped' as const;
          }),
        ),
      );
      published += results.filter((r) => r === 'published').length;
      await setBacklog(deps.pool, mailbox.id, Math.max(0, remaining - published));

      await deps.pool.query('select pg_notify($1, $2)', [
        'loop_events',
        JSON.stringify({
          type: 'scan.progress',
          user_id: mailbox.user_id,
          read: published,
          remaining: Math.max(0, remaining - published),
        }),
      ]);
    }
    pageToken = page.nextPageToken;
  } while (pageToken);

  await setBacklog(deps.pool, mailbox.id, 0);
  await markOk(deps.pool, mailbox.id);
  deps.log.info({ mailbox_id: mailbox.id, outcome: 'backfill_done', count: published, backfill: opts.backfill });
  return published;
}

/** Watches expire in 7 days; renew every 24 h and record the expiry. */
export async function renewWatch(
  deps: SyncDeps,
  mailbox: MailboxRow,
  topic: string,
  consecutiveFailures: number,
): Promise<{ ok: boolean; failures: number }> {
  try {
    const accessToken = await accessTokenFor(deps, mailbox);
    const res = await deps.google.watch(accessToken, topic);
    await deps.pool.query(
      `update mailbox_accounts set watch_expires_at = to_timestamp($2::bigint / 1000) where id = $1`,
      [mailbox.id, res.expiration],
    );
    return { ok: true, failures: 0 };
  } catch (err) {
    const failures = consecutiveFailures + 1;
    deps.log.warn({ mailbox_id: mailbox.id, outcome: 'watch_renew_failed', attempt: failures });
    if (failures >= CONNECTOR.WATCH_RENEW_FAILURES_BEFORE_POLLING) {
      await markError(deps.pool, mailbox.id, `watch renewal failed ${failures}×; polling instead`);
    }
    return { ok: false, failures };
  }
}

/**
 * Calendar.
 *
 * "Consider only events whose organiser domain or attendee domain matches a
 * known company domain, or whose iCalUID appeared in a mail we already placed."
 * Everything else in a calendar is somebody's life, and it is none of Loop's
 * business.
 */
export async function syncCalendar(deps: SyncDeps, mailbox: MailboxRow): Promise<number> {
  const accessToken = await accessTokenFor(deps, mailbox);
  const knownDomains = await deps.pool.query<{ domain: string }>(
    `select distinct c.domain from companies c
       join applications a on a.company_id = c.id
      where a.user_id = $1 and c.domain is not null`,
    [mailbox.user_id],
  );
  const domains = new Set(knownDomains.rows.map((r) => r.domain.toLowerCase()));

  let pageToken: string | undefined;
  let syncToken = mailbox.cursor?.syncToken;
  let relevant = 0;

  do {
    const page = await deps.google.listCalendarEvents(accessToken, {
      syncToken,
      timeMin: syncToken ? undefined : new Date(Date.now() - 90 * 86_400_000).toISOString(),
      pageToken,
    });

    for (const ev of page.items ?? []) {
      if (!isRelevant(ev, domains)) continue;
      relevant += 1;
      await publishCalendarEvent(deps, mailbox, ev);
    }

    pageToken = page.nextPageToken;
    if (page.nextSyncToken) syncToken = page.nextSyncToken;
  } while (pageToken);

  if (syncToken) await saveCursor(deps.pool, mailbox.id, { syncToken });
  await markOk(deps.pool, mailbox.id);
  return relevant;
}

function isRelevant(ev: CalendarEvent, companyDomains: ReadonlySet<string>): boolean {
  const addresses = [ev.organizer?.email, ...(ev.attendees ?? []).map((a) => a.email)].filter(Boolean);
  return addresses.some((a) => {
    const d = domainOfAddress(a!);
    return !!d && companyDomains.has(d);
  });
}

async function publishCalendarEvent(
  deps: SyncDeps,
  mailbox: MailboxRow,
  ev: CalendarEvent,
): Promise<void> {
  const id = `cal:${ev.id}`;
  const client = await deps.pool.connect();
  try {
    const seen = await client.query(
      `select body_sha256 from seen_messages where mailbox_id = $1 and provider_message_id = $2`,
      [mailbox.id, id],
    );
    const cancelled = ev.status === 'cancelled';
    const startsAt = ev.start?.dateTime ?? (ev.start?.date ? `${ev.start.date}T09:00:00Z` : null);
    if (!startsAt) return;

    const raw: RawMessage = {
      user_id: mailbox.user_id,
      mailbox_id: mailbox.id,
      provider_message_id: id,
      thread_id: null,
      received_at: new Date().toISOString(),
      headers: {
        message_id: id,
        from: ev.organizer?.email ?? '',
        to: (ev.attendees ?? []).map((a) => a.email ?? '').filter(Boolean),
        subject: ev.summary ?? 'Calendar event',
        date: startsAt,
      },
      text: ev.summary ?? '',
      body_sha256: id,
      invite: {
        uid: ev.iCalUID ?? ev.id,
        summary: ev.summary ?? null,
        starts_at: new Date(startsAt).toISOString(),
        ends_at: ev.end?.dateTime ? new Date(ev.end.dateTime).toISOString() : null,
        location: ev.location ?? null,
        organiser: ev.organizer?.email ?? null,
        attendees: (ev.attendees ?? []).map((a) => a.email ?? '').filter(Boolean),
        // "Cancellations matter as much as creations."
        status: cancelled ? 'cancelled' : 'confirmed',
        method: cancelled ? 'CANCEL' : 'REQUEST',
      },
    };

    // A cancellation for an event already seen must still get through, so the
    // seen-check is on (id, state) rather than id alone.
    const stateKey = cancelled ? `${id}:cancelled` : id;
    if (seen.rowCount && !cancelled) return;

    await client.query('begin');
    await client.query(
      `insert into seen_messages (mailbox_id, provider_message_id, user_id, body_sha256, received_at)
       values ($1,$2,$3,$4::bytea,$5) on conflict do nothing`,
      [mailbox.id, stateKey, mailbox.user_id, Buffer.from(stateKey), raw.received_at],
    );
    await publish(client, QUEUES.raw, { ...raw, provider_message_id: stateKey });
    await client.query('commit');
  } catch (err) {
    await client.query('rollback').catch(() => undefined);
    throw err;
  } finally {
    client.release();
  }
}
