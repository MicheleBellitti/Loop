#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';

/**
 * `npm run dev` — every service in one terminal, with its own database role so
 * row-level security applies exactly as it does in production.
 *
 * Under compose every service is handed its environment by the orchestrator.
 * Here nothing does, so the parent loads `.env` once and the children inherit
 * it — otherwise every service boots and immediately dies on a missing
 * DATABASE_URL, which is a confusing way to learn that a file was not read.
 */
if (existsSync('.env')) {
  process.loadEnvFile('.env');
} else {
  console.error('no .env found — copy .env.example and fill it in');
  process.exit(1);
}
const SERVICES = [
  ['gateway',    'services/gateway/src/index.ts',    'loop_gateway',    '3000'],
  ['connector',  'services/connector/src/index.ts',  'loop_connector',  '9105'],
  ['classifier', 'services/classifier/src/index.ts', 'loop_classifier', '9101'],
  ['extractor',  'services/extractor/src/index.ts',  'loop_extractor',  '9102'],
  ['resolver',   'services/resolver/src/index.ts',   'loop_resolver',   '9103'],
  ['pipeline',   'services/pipeline/src/index.ts',   'loop_pipeline',   '9104'],
  ['nudge',      'services/nudge/src/index.ts',      'loop_nudge',      '9106'],
  ['notifier',   'services/notifier/src/index.ts',   'loop_notifier',   '9107'],
];

const WIDTH = Math.max(...SERVICES.map(([n]) => n.length));
const children = [];

for (const [name, file, role, port] of SERVICES) {
  const child = spawn('node', ['--import', 'tsx', file], {
    env: { ...process.env, DB_ROLE: role, HEALTH_PORT: port },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const prefix = `${name.padEnd(WIDTH)} │ `;
  const pipe = (stream, out) =>
    stream.on('data', (d) =>
      out.write(String(d).split('\n').filter(Boolean).map((l) => prefix + l).join('\n') + '\n'),
    );
  pipe(child.stdout, process.stdout);
  pipe(child.stderr, process.stderr);
  child.on('exit', (code) => console.error(`${prefix}exited with ${code}`));
  children.push(child);
}

const stop = () => { for (const c of children) c.kill('SIGTERM'); };
process.on('SIGINT', () => { stop(); process.exit(0); });
process.on('SIGTERM', () => { stop(); process.exit(0); });
