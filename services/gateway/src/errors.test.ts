import { describe, expect, it } from 'vitest';
import Fastify from 'fastify';
import fastifyStatic from '@fastify/static';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * The regression for a leak that a spec review would not have caught.
 *
 * Registering `@fastify/static` after the routes makes a *later*
 * `setErrorHandler` silently ineffective, and Fastify's default handler
 * serialises the exception message straight to the client. In practice that
 * returned SQL text and Postgres error codes from `/api/stats` — found by
 * running the thing, not by reading it.
 *
 * The fix is ordering, which is exactly the kind of thing that gets undone by a
 * later refactor. Hence a test.
 */

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');

const quiet = () => ({
  error: { code: 'internal', message: 'something failed' },
});

async function buildApp(handlerFirst: boolean) {
  const app = Fastify();
  const setHandler = () =>
    app.setErrorHandler((_err, _req, reply) => reply.code(500).send(quiet()));

  if (handlerFirst) setHandler();
  app.get('/boom', async () => {
    throw new Error('column r.created_at does not exist');
  });
  await app.register(fastifyStatic, {
    root: join(ROOT, 'client'),
    prefix: '/',
    wildcard: false,
  });
  if (!handlerFirst) setHandler();

  await app.listen({ port: 0, host: '127.0.0.1' });
  const address = app.server.address();
  const port = typeof address === 'object' && address ? address.port : 0;
  return { app, port };
}

describe('the error handler', () => {
  it('never returns the exception message when registered first', async () => {
    const { app, port } = await buildApp(true);
    try {
      const res = await fetch(`http://127.0.0.1:${port}/boom`);
      const body = await res.text();
      expect(res.status).toBe(500);
      expect(body).not.toContain('created_at');
      expect(JSON.parse(body)).toEqual(quiet());
    } finally {
      await app.close();
    }
  });

  it('is defeated when registered after @fastify/static — the bug this pins', async () => {
    const { app, port } = await buildApp(false);
    try {
      const res = await fetch(`http://127.0.0.1:${port}/boom`);
      const body = await res.text();
      // Documenting the trap rather than asserting it is fine: if a future
      // Fastify fixes this, the assertion below fails and the ordering
      // requirement — and this comment — can be relaxed deliberately.
      expect(body).toContain('created_at');
    } finally {
      await app.close();
    }
  });
});
