import { parseQuietHours, type QuietHours } from '@loop/domain';

/**
 * Environment, read once and validated loudly.
 *
 * The two settings with a safety story get real checks rather than defaults:
 * `DEBUG_RETAIN_BODIES_DAYS` is capped at 7 because §03 caps it, and
 * `ALLOW_HOSTED_MODEL` must be explicitly true because §03 says it MUST default
 * false — a typo in an env file should not move email text off the box.
 */

export interface Config {
  databaseUrl: string;
  publicOrigin: string;
  port: number;
  google: {
    clientId: string | null;
    clientSecret: string | null;
    redirectUri: string;
    pubsubTopic: string | null;
  };
  model: {
    baseUrl: string | null;
    name: string;
    timeoutMs: number;
    allowHosted: boolean;
  };
  vapid: { publicKey: string | null; privateKey: string | null; subject: string };
  quietHours: QuietHours;
  dailyPushSlot: string;
  retainBodiesDays: number;
  webauthn: { rpId: string; rpName: string };
  sessionSecret: string | null;
}

function int(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return fallback;
  const n = Number(raw);
  if (!Number.isFinite(n)) throw new Error(`${name} must be a number, got ${raw}`);
  return n;
}

function bool(name: string, fallback = false): boolean {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return fallback;
  return raw === 'true' || raw === '1';
}

export function loadConfig(env = process.env): Config {
  const retainBodiesDays = int('DEBUG_RETAIN_BODIES_DAYS', 0);
  if (retainBodiesDays < 0 || retainBodiesDays > 7) {
    throw new Error(`DEBUG_RETAIN_BODIES_DAYS is capped at 7, got ${retainBodiesDays}`);
  }

  const allowHosted = bool('ALLOW_HOSTED_MODEL', false);
  const modelBaseUrl = env.MODEL_BASE_URL?.trim() || null;
  if (modelBaseUrl && !allowHosted && !/^https?:\/\/(llama|vllm|localhost|127\.0\.0\.1|\[::1\])/.test(modelBaseUrl)) {
    throw new Error(
      `MODEL_BASE_URL points off this box (${modelBaseUrl}) but ALLOW_HOSTED_MODEL is false. ` +
        'Enabling a hosted model is a per-user consent decision, not an env-file typo.',
    );
  }

  const port = int('PORT', 3000);
  const publicOrigin = env.PUBLIC_ORIGIN ?? `http://localhost:${port}`;

  return {
    databaseUrl: env.DATABASE_URL ?? '',
    publicOrigin,
    port,
    google: {
      clientId: env.GOOGLE_CLIENT_ID?.trim() || null,
      clientSecret: env.GOOGLE_CLIENT_SECRET?.trim() || null,
      redirectUri: env.GOOGLE_REDIRECT_URI ?? `${publicOrigin}/api/mailboxes/gmail/callback`,
      pubsubTopic: env.GOOGLE_PUBSUB_TOPIC?.trim() || null,
    },
    model: {
      baseUrl: modelBaseUrl,
      name: env.MODEL_NAME ?? 'qwen2.5-7b-instruct',
      timeoutMs: int('MODEL_TIMEOUT_MS', 30_000),
      allowHosted,
    },
    vapid: {
      publicKey: env.VAPID_PUBLIC?.trim() || null,
      privateKey: env.VAPID_PRIVATE?.trim() || null,
      subject: env.VAPID_SUBJECT ?? 'mailto:loop@localhost',
    },
    quietHours: parseQuietHours(env.QUIET_HOURS ?? '21:00-08:00'),
    dailyPushSlot: env.DAILY_PUSH_SLOT ?? '18:00',
    retainBodiesDays,
    webauthn: { rpId: env.RP_ID ?? 'localhost', rpName: env.RP_NAME ?? 'Loop' },
    sessionSecret: env.SESSION_SECRET?.trim() || null,
  };
}
