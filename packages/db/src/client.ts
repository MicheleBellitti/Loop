import pg from 'pg';

const { Pool } = pg;

/**
 * Postgres access, with the tenant GUC as the only way in.
 *
 * Row-level security is only as good as the discipline that sets
 * `loop.user_id`, so the discipline is removed: every user-scoped query goes
 * through `withUser`, which opens a transaction, sets the GUC locally, and lets
 * the policy do the filtering. Nothing in this codebase composes a `where
 * user_id = $1` by hand, because the one time it is forgotten is the one time
 * it matters.
 */

export type Sql = pg.PoolClient;

export interface DbOptions {
  connectionString?: string;
  max?: number;
  applicationName?: string;
  /**
   * Raise `idle_in_transaction_session_timeout` for this pool alone. Only a
   * service that holds a transaction open across a network call has any
   * business doing so, and it owns the consequence rather than spreading it.
   */
  idleInTransactionTimeoutMs?: number;
  /** Called when a pooled client fails while idle. See `createPool`. */
  onError?: (err: Error) => void;
}

let sharedPool: pg.Pool | null = null;

export function createPool(opts: DbOptions = {}): pg.Pool {
  const connectionString = opts.connectionString ?? process.env.DATABASE_URL;
  if (!connectionString) throw new Error('DATABASE_URL is not set');
  const p = new Pool({
    connectionString,
    max: opts.max ?? 10,
    application_name: opts.applicationName ?? 'loop',
    // A query that has been running for two minutes is a bug, not a slow query.
    statement_timeout: 120_000,
    // An open transaction doing nothing still holds its locks and still pins
    // the vacuum horizon, so the default stays tight. The one service that
    // legitimately needs longer — the extractor, which awaits the model from
    // inside its transaction — raises its own pool and nobody else's.
    idle_in_transaction_session_timeout: opts.idleInTransactionTimeoutMs ?? 30_000,
  });

  // Postgres can terminate a backend under a client that is sitting idle in the
  // pool: a restart, a failover, an administrative pg_terminate_backend, an
  // idle-in-transaction timeout. node-pg reports that on the pool, and an
  // 'error' event with no listener is an uncaught exception — which is to say
  // the whole service dies because one pooled connection did. The connection is
  // already gone and the pool will replace it, so the only thing left to do is
  // say so out loud; swallowing it silently is how this becomes unfindable.
  p.on('error', opts.onError ?? ((err) => console.error('idle database client failed', err)));

  return p;
}

export function pool(opts: DbOptions = {}): pg.Pool {
  sharedPool ??= createPool(opts);
  return sharedPool;
}

export async function closePool(): Promise<void> {
  if (sharedPool) {
    await sharedPool.end();
    sharedPool = null;
  }
}

/** A transaction with no tenant set. Migrations, health checks, cron only. */
export async function withTransaction<T>(
  run: (sql: Sql) => Promise<T>,
  p: pg.Pool = pool(),
): Promise<T> {
  const client = await p.connect();
  try {
    await client.query('begin');
    const out = await run(client);
    await client.query('commit');
    return out;
  } catch (err) {
    await client.query('rollback').catch(() => undefined);
    throw err;
  } finally {
    client.release();
  }
}

/**
 * Everything user-facing runs in here. `set_config(..., true)` is transaction
 * -local, so a pooled connection cannot leak one user's tenant into the next
 * request even if something throws.
 */
export async function withUser<T>(
  userId: string,
  run: (sql: Sql) => Promise<T>,
  p: pg.Pool = pool(),
  opts: { role?: string } = {},
): Promise<T> {
  // A superuser — which the owner role is — bypasses row-level security
  // entirely, policies and FORCE both. So a service that connects as the owner
  // has no tenant isolation at all, however carefully the policies are written.
  // `DB_ROLE` drops the transaction to the service's own role, and the local
  // scope means the connection returns to the pool as it was found.
  const role = opts.role ?? process.env.DB_ROLE;
  return withTransaction(async (sql) => {
    if (role) await sql.query(`set local role ${quoteIdent(role)}`);
    await sql.query('select set_config($1, $2, true)', ['loop.user_id', userId]);
    return run(sql);
  }, p);
}

/** Identifiers cannot be bind parameters; this is the only place one is built. */
function quoteIdent(name: string): string {
  if (!/^[a-z_][a-z0-9_]*$/i.test(name)) throw new Error(`unsafe role name: ${name}`);
  return `"${name}"`;
}

/** Read-only variant, so a handler that should not write cannot. */
export async function withUserReadOnly<T>(
  userId: string,
  run: (sql: Sql) => Promise<T>,
  p: pg.Pool = pool(),
  opts: { role?: string } = {},
): Promise<T> {
  const role = opts.role ?? process.env.DB_ROLE;
  return withTransaction(async (sql) => {
    await sql.query('set transaction read only');
    if (role) await sql.query(`set local role ${quoteIdent(role)}`);
    await sql.query('select set_config($1, $2, true)', ['loop.user_id', userId]);
    return run(sql);
  }, p);
}

/** Rows come back as `unknown`; callers narrow with a zod schema at the edge. */
export async function all<T>(sql: Sql, text: string, values: unknown[] = []): Promise<T[]> {
  const res = await sql.query(text, values as never[]);
  return res.rows as T[];
}

export async function one<T>(sql: Sql, text: string, values: unknown[] = []): Promise<T | null> {
  const rows = await all<T>(sql, text, values);
  return rows[0] ?? null;
}

export async function exactlyOne<T>(sql: Sql, text: string, values: unknown[] = []): Promise<T> {
  const rows = await all<T>(sql, text, values);
  if (rows.length !== 1) throw new Error(`expected exactly one row, got ${rows.length}`);
  return rows[0]!;
}
