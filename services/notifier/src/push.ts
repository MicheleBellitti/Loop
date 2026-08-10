import webpush from 'web-push';

/**
 * Web Push (VAPID) straight from the notifier — no vendor, works on installed
 * PWAs, €0 a month.
 *
 * This is the only outbound-message code in the repository, and it is push, not
 * mail: `npm run lint:no-send-path` asserts that no SMTP or Gmail send API is
 * reachable from anywhere in the tree. Loop drafts follow-ups; it never has the
 * right to send one.
 */

export interface PushKeys {
  endpoint: string;
  keys: { p256dh: string; auth: string };
}

export interface PushPayload {
  title: string;
  body: string;
  url: string;
  tag: string;
}

export interface VapidConfig {
  publicKey: string | null;
  privateKey: string | null;
  subject: string;
}

let configured = false;

export function configureVapid(config: VapidConfig): boolean {
  if (!config.publicKey || !config.privateKey) return false;
  if (!configured) {
    webpush.setVapidDetails(config.subject, config.publicKey, config.privateKey);
    configured = true;
  }
  return true;
}

/** Generated once at first boot if absent, then persisted to the env file. */
export function generateVapidKeys(): { publicKey: string; privateKey: string } {
  return webpush.generateVAPIDKeys();
}

export type PushResult = 'ok' | 'gone' | 'failed' | 'unconfigured';

export async function sendPush(
  sub: PushKeys,
  payload: PushPayload,
  config: VapidConfig,
): Promise<PushResult> {
  if (!configureVapid(config)) return 'unconfigured';
  try {
    await webpush.sendNotification(
      { endpoint: sub.endpoint, keys: sub.keys },
      JSON.stringify(payload),
      { TTL: 12 * 3600 },
    );
    return 'ok';
  } catch (err) {
    const status = (err as { statusCode?: number }).statusCode;
    // 404/410: the subscription is gone for good.
    if (status === 404 || status === 410) return 'gone';
    return 'failed';
  }
}
