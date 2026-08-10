import { createServer, type Server } from 'node:http';
import type pg from 'pg';
import { closePool, createPool } from '@loop/db';
import { createLogger, type Logger } from './log.js';
import { loadConfig, type Config } from './config.js';
import { renderMetrics } from './metrics.js';

/**
 * The bootstrap every consumer service shares: a pool, a logger, a health
 * endpoint on the internal network, and a shutdown that drains rather than
 * drops. Services are separate containers precisely so they restart and log
 * independently — this is the part that makes that true.
 */

export interface ServiceContext {
  name: string;
  config: Config;
  pool: pg.Pool;
  log: Logger;
}

export interface ServiceOptions {
  name: string;
  /** Internal health/metrics port. Never published outside the compose network. */
  healthPort?: number;
  /** Extra checks folded into /health/deep. */
  deepCheck?: (ctx: ServiceContext) => Promise<Record<string, unknown>>;
}

export async function startService(
  opts: ServiceOptions,
  run: (ctx: ServiceContext) => Promise<() => Promise<void>>,
): Promise<void> {
  const config = loadConfig();
  const log = createLogger(opts.name);
  const pool = createPool({ applicationName: `loop-${opts.name}` });
  const ctx: ServiceContext = { name: opts.name, config, pool, log };

  let health: Server | null = null;
  if (opts.healthPort) {
    health = createServer((req, res) => {
      const url = req.url ?? '/';
      if (url === '/metrics') {
        res.writeHead(200, { 'content-type': 'text/plain; version=0.0.4' });
        res.end(renderMetrics());
        return;
      }
      if (url === '/health') {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ ok: true, service: opts.name }));
        return;
      }
      if (url === '/health/deep') {
        void (async () => {
          try {
            await pool.query('select 1');
            const extra = opts.deepCheck ? await opts.deepCheck(ctx) : {};
            res.writeHead(200, { 'content-type': 'application/json' });
            res.end(JSON.stringify({ ok: true, service: opts.name, ...extra }));
          } catch (err) {
            res.writeHead(503, { 'content-type': 'application/json' });
            res.end(JSON.stringify({ ok: false, error: (err as Error).message }));
          }
        })();
        return;
      }
      res.writeHead(404);
      res.end();
    });
    health.listen(opts.healthPort);
  }

  const stop = await run(ctx);
  log.info({ msg: 'started' });

  let shuttingDown = false;
  const shutdown = async (signal: string): Promise<void> => {
    if (shuttingDown) return;
    shuttingDown = true;
    log.info({ msg: 'shutting down', reason: signal });
    try {
      await stop();
    } catch (err) {
      log.error({ msg: 'shutdown handler failed', error: (err as Error).message });
    }
    health?.close();
    await closePool().catch(() => undefined);
    await pool.end().catch(() => undefined);
    process.exit(0);
  };

  process.on('SIGTERM', () => void shutdown('SIGTERM'));
  process.on('SIGINT', () => void shutdown('SIGINT'));
  process.on('unhandledRejection', (err) => {
    log.error({ msg: 'unhandled rejection', error: String(err) });
  });
}
