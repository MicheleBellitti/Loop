import type { FastifyInstance, FastifyReply } from 'fastify';
import pg from 'pg';
import type { Config, Logger } from '@loop/runtime';

/**
 * `GET /api/stream`.
 *
 * "The clients hold an SSE connection and apply `application.changed`,
 * `scan.progress`, `review.added` and `mailbox.status` in place. A stage change
 * arriving while the user is looking at the list should animate the row, not
 * reload the view."
 *
 * The events originate in other containers, so they arrive over LISTEN/NOTIFY
 * and are fanned out to whichever browsers this gateway happens to be holding.
 */

type Client = { userId: string; reply: FastifyReply };

const clients = new Set<Client>();

export function broadcast(userId: string, event: string, data: unknown): void {
  const frame = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const client of clients) {
    if (client.userId !== userId) continue;
    client.reply.raw.write(frame);
  }
}

export function registerSse(
  app: FastifyInstance,
  deps: { pool: pg.Pool; config: Config; log: Logger },
): void {
  const listener = new pg.Client({ connectionString: deps.config.databaseUrl });
  void listener.connect().then(async () => {
    await listener.query('listen loop_events');
    listener.on('notification', (msg) => {
      try {
        const payload = JSON.parse(msg.payload ?? '{}') as { type?: string; user_id?: string };
        if (!payload.type || !payload.user_id) return;
        broadcast(payload.user_id, payload.type, payload);
      } catch {
        // A malformed notification is not worth taking the gateway down for.
      }
    });
  });

  app.get('/api/stream', async (req, reply) => {
    const userId = req.session!.userId;

    reply.raw.writeHead(200, {
      'content-type': 'text/event-stream',
      'cache-control': 'no-cache, no-transform',
      connection: 'keep-alive',
      // Caddy and every other proxy in front of this must not buffer a stream.
      'x-accel-buffering': 'no',
    });
    reply.raw.write(': connected\n\n');

    const client: Client = { userId, reply };
    clients.add(client);

    // A comment frame every twenty-five seconds: proxies drop idle connections
    // at thirty, and a silently dead stream is the same failure mode as a
    // silently dead connector.
    const heartbeat = setInterval(() => {
      reply.raw.write(': ping\n\n');
    }, 25_000);

    req.raw.on('close', () => {
      clearInterval(heartbeat);
      clients.delete(client);
    });

    // Fastify must not try to serialise a response for a hijacked socket.
    return reply.hijack();
  });
}
