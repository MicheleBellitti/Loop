import type pg from 'pg';
import { stripBodies } from '@loop/domain';

/**
 * pgmq, wrapped.
 *
 * One queue per stage of the pipeline, transactional with the data it
 * describes. §05 names the queues with dots (`raw.message`); pgmq derives table
 * names from the queue name, so the underscore forms below are the same five.
 */

export const QUEUES = {
  raw: 'raw_message', // connector  → classifier
  candidate: 'candidate_message', // classifier → extractor
  signal: 'signal_extracted', // extractor  → resolver
  event: 'event_pending', // resolver   → pipeline
  notify: 'notify_pending', // nudge      → notifier
} as const;

export type QueueName = (typeof QUEUES)[keyof typeof QUEUES];

export const VISIBILITY_TIMEOUT_S = 60;
export const MAX_ATTEMPTS = 5;

export interface QueueMessage<T> {
  msgId: string;
  readCount: number;
  enqueuedAt: Date;
  body: T;
}

interface RawRow {
  msg_id: string;
  read_ct: number;
  enqueued_at: string;
  message: unknown;
}

type Queryable = Pick<pg.Pool | pg.PoolClient, 'query'>;

/**
 * Publishing inside the caller's transaction is the point: a message is only
 * visible to the next stage if the row it describes actually committed.
 */
export async function publish<T>(sql: Queryable, queue: QueueName, body: T): Promise<string> {
  const res = await sql.query<{ send: string }>('select mq.send($1, $2) as send', [
    queue,
    JSON.stringify(body),
  ]);
  return res.rows[0]!.send;
}

export async function publishMany<T>(sql: Queryable, queue: QueueName, bodies: T[]): Promise<string[]> {
  if (bodies.length === 0) return [];
  const res = await sql.query<{ send_batch: string }>(
    'select unnest(mq.send_batch($1, $2::jsonb[])) as send_batch',
    [queue, bodies.map((b) => JSON.stringify(b))],
  );
  return res.rows.map((r) => r.send_batch);
}

export async function read<T>(
  sql: Queryable,
  queue: QueueName,
  qty = 1,
  vt = VISIBILITY_TIMEOUT_S,
): Promise<QueueMessage<T>[]> {
  const res = await sql.query<RawRow>('select * from mq.read($1, $2, $3)', [queue, vt, qty]);
  return res.rows.map((r) => ({
    msgId: String(r.msg_id),
    readCount: r.read_ct,
    enqueuedAt: new Date(r.enqueued_at),
    body: r.message as T,
  }));
}

export async function ack(sql: Queryable, queue: QueueName, msgId: string): Promise<void> {
  await sql.query('select mq.delete($1, $2::bigint)', [queue, msgId]);
}

/**
 * Dead-letter.
 *
 * §05 says "dead-letter with the original payload", and §04 says no table ever
 * stores message bodies. A queue row is a table row, so the two only agree
 * while a message is in flight — the ack deletes it. The archive is the one
 * place a payload would sit indefinitely, so the body is stripped on the way
 * in. Nothing is lost operationally: `seen_messages` makes every message
 * replayable from the provider by id, which is the whole point of a replay log.
 */
export async function deadLetter<T>(
  sql: Queryable,
  queue: QueueName,
  msgId: string,
  body: T,
): Promise<void> {
  await sql.query('select mq.send($1, $2)', [`${queue}_dlq`, JSON.stringify(stripBodies(body))]);
  await sql.query('select mq.delete($1, $2::bigint)', [queue, msgId]);
}

export async function queueDepth(sql: Queryable, queue: QueueName): Promise<number> {
  const res = await sql.query<{ n: string }>(
    'select coalesce(queue_length, 0)::text as n from mq.metrics($1)',
    [queue],
  );
  return Number(res.rows[0]?.n ?? '0');
}

export async function oldestMessageAgeSeconds(sql: Queryable, queue: QueueName): Promise<number> {
  const res = await sql.query<{ age: string | null }>(
    'select coalesce(oldest_msg_age_sec, 0)::text as age from mq.metrics($1)',
    [queue],
  );
  return Number(res.rows[0]?.age ?? '0');
}

/**
 * Dead-letter depth. pgmq's archive lives in a per-queue table whose name
 * cannot be a bind parameter, so the identifier is quoted inside a SQL function
 * (migration 005) rather than interpolated here.
 */
export async function deadLetterCount(sql: Queryable, queue: QueueName): Promise<number> {
  const res = await sql.query<{ n: string }>(
    'select coalesce(queue_length, 0)::text as n from mq.metrics($1)',
    [`${queue}_dlq`],
  );
  return Number(res.rows[0]?.n ?? '0');
}

/** Every dead-letter queue, for the §16 alert on "dead-letter count > 0". */
export async function totalDeadLetters(sql: Queryable): Promise<number> {
  let total = 0;
  for (const q of Object.values(QUEUES)) total += await deadLetterCount(sql, q);
  return total;
}

export interface ConsumerOptions {
  queue: QueueName;
  /** How many messages one poll may claim. */
  batch?: number;
  /** Concurrency inside a batch. Ordering-sensitive consumers use 1. */
  concurrency?: number;
  pollIntervalMs?: number;
  maxAttempts?: number;
  visibilityTimeoutS?: number;
  onError?: (err: unknown, msg: QueueMessage<unknown>) => void;
  log?: (line: Record<string, unknown>) => void;
}

/**
 * The consumer loop every service shares.
 *
 * A handler that throws leaves the message invisible until the visibility
 * timeout expires, so it is retried; after `maxAttempts` reads it is
 * dead-lettered with its original payload rather than dropped. Every consumer
 * is expected to be idempotent on (mailbox_id, provider_message_id) — the
 * database's unique indexes are what make that true rather than hoped for.
 */
export function createConsumer(pool: pg.Pool, opts: ConsumerOptions) {
  const {
    queue,
    batch = 10,
    concurrency = 1,
    pollIntervalMs = 1_000,
    maxAttempts = MAX_ATTEMPTS,
    visibilityTimeoutS = VISIBILITY_TIMEOUT_S,
    log = () => undefined,
  } = opts;

  let running = false;
  let stopped: (() => void) | null = null;

  async function handleOne<T>(
    msg: QueueMessage<T>,
    handler: (body: T, msg: QueueMessage<T>, sql: pg.PoolClient) => Promise<void>,
  ): Promise<void> {
    const client = await pool.connect();
    const started = Date.now();
    try {
      await client.query('begin');
      await handler(msg.body, msg, client);
      await ack(client, queue, msg.msgId);
      await client.query('commit');
      log({ queue, msg_id: msg.msgId, outcome: 'ok', duration_ms: Date.now() - started });
    } catch (err) {
      await client.query('rollback').catch(() => undefined);
      opts.onError?.(err, msg as QueueMessage<unknown>);
      log({
        queue,
        msg_id: msg.msgId,
        outcome: 'error',
        read_ct: msg.readCount,
        duration_ms: Date.now() - started,
        error: (err as Error).message,
      });
      if (msg.readCount >= maxAttempts) {
        await deadLetter(pool, queue, msg.msgId, msg.body).catch(() => undefined);
        log({ queue, msg_id: msg.msgId, outcome: 'dead_letter', read_ct: msg.readCount });
      }
    } finally {
      client.release();
    }
  }

  return {
    async start<T>(
      handler: (body: T, msg: QueueMessage<T>, sql: pg.PoolClient) => Promise<void>,
    ): Promise<void> {
      running = true;
      while (running) {
        let messages: QueueMessage<T>[] = [];
        try {
          messages = await read<T>(pool, queue, batch, visibilityTimeoutS);
        } catch (err) {
          log({ queue, outcome: 'poll_error', error: (err as Error).message });
        }

        if (messages.length === 0) {
          await new Promise((r) => setTimeout(r, pollIntervalMs));
          continue;
        }

        if (concurrency <= 1) {
          for (const m of messages) await handleOne(m, handler);
        } else {
          for (let i = 0; i < messages.length; i += concurrency) {
            await Promise.all(messages.slice(i, i + concurrency).map((m) => handleOne(m, handler)));
          }
        }
      }
      stopped?.();
    },
    async stop(): Promise<void> {
      running = false;
      await new Promise<void>((resolve) => {
        stopped = resolve;
        setTimeout(resolve, pollIntervalMs + 500);
      });
    },
  };
}
