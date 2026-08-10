#!/usr/bin/env node
import { execFileSync } from 'node:child_process';

/**
 * The integration-test database.
 *
 * `up` starts the same image compose uses — the extensions and the pg_cron
 * preload have to be identical, or the tests prove something about a database
 * nobody deploys.
 */
const NAME = 'loop-pg-test';
const PORT = process.env.TEST_DB_PORT ?? '55432';
const IMAGE = 'loop-postgres:16';
const run = (args, opts = {}) => execFileSync('docker', args, { stdio: 'inherit', ...opts });

const action = process.argv[2] ?? 'up';

if (action === 'up') {
  try {
    execFileSync('docker', ['image', 'inspect', IMAGE], { stdio: 'ignore' });
  } catch {
    console.log(`building ${IMAGE}…`);
    run(['build', '-t', IMAGE, 'infra/postgres']);
  }
  try {
    execFileSync('docker', ['rm', '-f', NAME], { stdio: 'ignore' });
  } catch { /* not running */ }
  run(['run', '-d', '--name', NAME, '-e', 'POSTGRES_USER=loop', '-e', 'POSTGRES_PASSWORD=loop',
       '-e', 'POSTGRES_DB=loop', '-p', `${PORT}:5432`, IMAGE]);

  process.stdout.write('waiting for postgres');
  for (let i = 0; i < 60; i++) {
    try {
      execFileSync('docker', ['exec', NAME, 'pg_isready', '-U', 'loop', '-d', 'loop'], { stdio: 'ignore' });
      console.log(`\nready · TEST_DATABASE_URL=postgres://loop:loop@localhost:${PORT}/loop`);
      process.exit(0);
    } catch {
      process.stdout.write('.');
      execFileSync('sleep', ['1']);
    }
  }
  console.error('\npostgres did not become ready');
  process.exit(1);
}

if (action === 'down') {
  run(['rm', '-f', NAME]);
  process.exit(0);
}

console.error(`unknown action ${action}; use up or down`);
process.exit(1);
