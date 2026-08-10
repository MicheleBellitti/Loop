import { readFile, readdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import pg from 'pg';
import { createPool } from './client.js';

/**
 * The migration runner.
 *
 * §02 names node-pg-migrate. This is plain numbered SQL applied by sixty lines
 * instead, and the reason belongs in the PR as the spec asks: this schema's
 * hardest parts are RLS policies, per-role grants and a trigger that enforces
 * append-only. All three are *only* expressible as SQL, so a JavaScript DSL
 * around them would add a translation layer over the exact statements a
 * reviewer needs to read literally. The properties node-pg-migrate provides —
 * an applied-migrations table, ordering, one transaction per migration, a lock
 * so two boots cannot race — are reproduced here and tested.
 *
 * Each file is hashed on apply; changing an already-applied migration is a hard
 * error rather than a silent divergence between this box and the next one.
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const MIGRATIONS_DIR = join(HERE, '..', 'migrations');
const LOCK_KEY = 8_1966_0201; // arbitrary, stable

export interface MigrationResult {
  applied: string[];
  alreadyApplied: string[];
}

async function ensureTable(client: pg.PoolClient): Promise<void> {
  await client.query(`
    create table if not exists schema_migrations (
      name       text primary key,
      sha256     text not null,
      applied_at timestamptz not null default now()
    )
  `);
}

export async function migrate(
  opts: { connectionString?: string; dir?: string; log?: (m: string) => void } = {},
): Promise<MigrationResult> {
  const dir = opts.dir ?? MIGRATIONS_DIR;
  const log = opts.log ?? (() => undefined);
  const p = createPool({ connectionString: opts.connectionString, max: 1, applicationName: 'loop-migrate' });
  const client = await p.connect();
  const result: MigrationResult = { applied: [], alreadyApplied: [] };

  try {
    // A session-level advisory lock, so two services booting at once do not
    // both try to create the same type.
    await client.query('select pg_advisory_lock($1)', [LOCK_KEY]);
    await ensureTable(client);

    const files = (await readdir(dir)).filter((f) => f.endsWith('.sql')).sort();
    const { rows } = await client.query<{ name: string; sha256: string }>(
      'select name, sha256 from schema_migrations',
    );
    const seen = new Map(rows.map((r) => [r.name, r.sha256]));

    for (const name of files) {
      const body = await readFile(join(dir, name), 'utf8');
      const sha256 = createHash('sha256').update(body).digest('hex');
      const previous = seen.get(name);

      if (previous) {
        if (previous !== sha256) {
          throw new Error(
            `migration ${name} changed after it was applied (${previous.slice(0, 8)} → ${sha256.slice(0, 8)}). ` +
              'Write a new migration instead of editing history.',
          );
        }
        result.alreadyApplied.push(name);
        continue;
      }

      log(`applying ${name}`);
      try {
        await client.query('begin');
        await client.query(body);
        await client.query('insert into schema_migrations (name, sha256) values ($1, $2)', [name, sha256]);
        await client.query('commit');
        result.applied.push(name);
      } catch (err) {
        await client.query('rollback').catch(() => undefined);
        throw new Error(`migration ${name} failed: ${(err as Error).message}`, { cause: err });
      }
    }
    return result;
  } finally {
    await client.query('select pg_advisory_unlock($1)', [LOCK_KEY]).catch(() => undefined);
    client.release();
    await p.end();
  }
}

// `node packages/db/src/migrate.ts` runs it directly.
if (process.argv[1] && import.meta.url.endsWith(process.argv[1].split('/').pop() ?? '')) {
  const res = await migrate({ log: (m) => console.log(m) });
  console.log(
    res.applied.length ? `applied ${res.applied.length} migration(s)` : 'schema already up to date',
  );
  process.exit(0);
}
