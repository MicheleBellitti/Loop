import { redact } from '@loop/db';

/**
 * Structured JSON logs, one line per message processed.
 *
 * "…never subject lines, never sender addresses, never body fragments. That
 * single line is enough to debug extraction, which is the point of keeping it
 * clean enough to be safe to keep." (Spec §16)
 *
 * The allow-list below is the enforcement of that sentence: a field not named
 * here does not reach the log, so adding `subject` to a debug line is a change
 * someone has to make deliberately, in this file, in a diff.
 */

const ALLOWED_FIELDS = new Set([
  'service', 'level', 'time', 'msg',
  'mailbox_id', 'provider_message_id', 'user_id', 'application_id', 'company_id',
  'thread_id', 'queue', 'msg_id', 'rung', 'outcome', 'confidence', 'score',
  'duration_ms', 'read_ct', 'attempt', 'count', 'depth', 'error', 'code',
  'intent', 'vendor', 'decision', 'rule', 'status', 'reason', 'cosine',
  'threshold', 'candidates', 'stage', 'phase', 'event_type', 'violations',
  'backfill', 'history_id', 'sync_token', 'batch', 'endpoint', 'method',
]);

export type LogLevel = 'debug' | 'info' | 'warn' | 'error';

export interface Logger {
  debug(fields: Record<string, unknown>): void;
  info(fields: Record<string, unknown>): void;
  warn(fields: Record<string, unknown>): void;
  error(fields: Record<string, unknown>): void;
  child(base: Record<string, unknown>): Logger;
}

const LEVELS: Record<LogLevel, number> = { debug: 10, info: 20, warn: 30, error: 40 };

function minLevel(): number {
  return LEVELS[(process.env.LOG_LEVEL as LogLevel) ?? 'info'] ?? LEVELS.info;
}

export function createLogger(service: string, base: Record<string, unknown> = {}): Logger {
  const emit = (level: LogLevel, fields: Record<string, unknown>): void => {
    if (LEVELS[level] < minLevel()) return;
    const line: Record<string, unknown> = {
      time: new Date().toISOString(),
      level,
      service,
      ...base,
      ...fields,
    };
    const safe: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(line)) {
      if (!ALLOWED_FIELDS.has(k)) continue;
      safe[k] = typeof v === 'object' && v !== null ? redact(v) : v;
    }
    const stream = level === 'error' || level === 'warn' ? process.stderr : process.stdout;
    stream.write(`${JSON.stringify(safe)}\n`);
  };

  return {
    debug: (f) => emit('debug', f),
    info: (f) => emit('info', f),
    warn: (f) => emit('warn', f),
    error: (f) => emit('error', f),
    child: (extra) => createLogger(service, { ...base, ...extra }),
  };
}
