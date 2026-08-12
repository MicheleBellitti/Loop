import { describe, expect, it, vi } from 'vitest';
import { createPool } from './client.js';

/**
 * These construct a pool and never connect, so they belong in the unit suite:
 * `new Pool()` is lazy, and what is under test here is the wiring around it
 * rather than anything Postgres does.
 */

const CONNECTION = 'postgres://loop:loop@localhost:1/loop';

describe('createPool', () => {
  it('survives a client that fails while idle', async () => {
    const onError = vi.fn();
    const pool = createPool({ connectionString: CONNECTION, onError });

    // What Postgres does on a restart, a failover, an administrative
    // pg_terminate_backend or an idle-in-transaction timeout. Unlistened, this
    // is an uncaught exception and the service is gone — the pool being an
    // EventEmitter is the whole reason a lost connection used to be fatal.
    expect(() => pool.emit('error', new Error('terminating connection'))).not.toThrow();
    expect(onError).toHaveBeenCalledOnce();

    await pool.end();
  });

  it('installs a handler even when the caller supplies none', async () => {
    const pool = createPool({ connectionString: CONNECTION });
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    expect(() => pool.emit('error', new Error('terminating connection'))).not.toThrow();
    expect(spy).toHaveBeenCalledOnce();

    spy.mockRestore();
    await pool.end();
  });

  it('keeps idle transactions on a short leash by default', async () => {
    const pool = createPool({ connectionString: CONNECTION });
    // An open transaction with nothing to do holds its locks and pins the
    // vacuum horizon. Only a service that awaits the network inside one has any
    // reason to raise this, and it must not raise it for everybody else.
    expect(pool.options.idle_in_transaction_session_timeout).toBe(30_000);
    await pool.end();
  });

  it('lets a single caller raise it without touching the default', async () => {
    const raised = createPool({ connectionString: CONNECTION, idleInTransactionTimeoutMs: 90_000 });
    const other = createPool({ connectionString: CONNECTION });

    expect(raised.options.idle_in_transaction_session_timeout).toBe(90_000);
    expect(other.options.idle_in_transaction_session_timeout).toBe(30_000);

    await Promise.all([raised.end(), other.end()]);
  });
});
