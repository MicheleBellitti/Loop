#!/usr/bin/env node
import { createServer } from 'node:http';
import { readFile, readdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * The stub Google server §17 asks for.
 *
 * "A stub OAuth/Gmail server that replays fixture messages through the real
 * connector path." It speaks the six endpoints the connector uses, serving the
 * golden corpus as if it were a mailbox — so the connector, classifier,
 * extractor, resolver and pipeline all run their real code against it, and the
 * only thing faked is Google.
 *
 * This is the reason the client in packages/google is hand-rolled over `fetch`
 * with a configurable base URL: pointing GOOGLE_API_BASE here is the whole
 * setup, and no library needs mocking.
 */

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const PORT = Number(process.env.STUB_PORT ?? 8787);

const messages = new Map(); // id → { raw, internalDate, threadId }

async function loadFixtures() {
  for (const dir of ['fixtures/ats', 'fixtures/negatives']) {
    for (const name of await readdir(join(ROOT, dir))) {
      if (!name.endsWith('.eml')) continue;
      const raw = await readFile(join(ROOT, dir, name), 'utf8');
      const id = name.replace(/\.eml$/, '');
      messages.set(id, { raw, internalDate: String(Date.parse('2026-07-30T09:12:00Z')), threadId: `t-${id}` });
    }
  }
}

/** .eml → the payload shape `messages.get(format=full)` returns. */
function toGmailMessage(id, entry) {
  const split = entry.raw.indexOf('\n\n');
  const headerBlock = entry.raw.slice(0, split);
  let body = entry.raw.slice(split + 2);

  const headers = [];
  for (const line of headerBlock.replace(/\n[ \t]/g, ' ').split('\n')) {
    const idx = line.indexOf(':');
    if (idx > 0) headers.push({ name: line.slice(0, idx).trim(), value: line.slice(idx + 1).trim() });
  }

  const contentType = headers.find((h) => h.name.toLowerCase() === 'content-type')?.value ?? 'text/plain';
  const boundary = /boundary="([^"]+)"/.exec(contentType)?.[1];
  const b64 = (s) => Buffer.from(s, 'utf8').toString('base64url');

  if (boundary) {
    const chunks = body.split(`--${boundary}`);
    const parts = [];
    for (const chunk of chunks) {
      const type = /content-type:\s*([^;\n]+)/i.exec(chunk)?.[1]?.trim();
      if (!type) continue;
      const inner = chunk.slice(chunk.indexOf('\n\n') + 2).trim();
      parts.push({ mimeType: type, body: { data: b64(inner) }, headers: [] });
    }
    return { id, threadId: entry.threadId, internalDate: entry.internalDate, payload: { mimeType: 'multipart/mixed', headers, parts } };
  }

  return {
    id,
    threadId: entry.threadId,
    internalDate: entry.internalDate,
    payload: { mimeType: contentType.split(';')[0].trim(), headers, body: { data: b64(body) } },
  };
}

const json = (res, status, payload) => {
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify(payload));
};

await loadFixtures();

const server = createServer((req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const path = url.pathname;

  // OAuth: any code exchanges, any refresh succeeds. The stub is not testing
  // Google's authentication, it is testing what happens after it.
  if (path === '/token') {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () =>
      json(res, 200, {
        access_token: 'stub-access-token',
        refresh_token: 'stub-refresh-token',
        expires_in: 3600,
        scope: 'https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/calendar.readonly',
        token_type: 'Bearer',
      }),
    );
    return;
  }

  if (path === '/gmail/v1/users/me/profile') {
    return json(res, 200, { emailAddress: 'you@example.com', historyId: '1000' });
  }

  if (path === '/gmail/v1/users/me/messages') {
    const ids = [...messages.keys()].map((id) => ({ id, threadId: `t-${id}` }));
    return json(res, 200, { messages: ids, resultSizeEstimate: ids.length });
  }

  const detail = /^\/gmail\/v1\/users\/me\/messages\/(.+)$/.exec(path);
  if (detail) {
    const id = decodeURIComponent(detail[1]);
    const entry = messages.get(id);
    if (!entry) return json(res, 404, { error: { code: 404, message: 'not found' } });
    return json(res, 200, toGmailMessage(id, entry));
  }

  if (path === '/gmail/v1/users/me/history') {
    // Nothing new since the backfill: live sync is a no-op in the stub.
    return json(res, 200, { historyId: '1000' });
  }

  if (path === '/gmail/v1/users/me/watch') {
    return json(res, 200, { historyId: '1000', expiration: String(Date.now() + 7 * 86_400_000) });
  }

  if (path === '/calendar/v3/calendars/primary/events') {
    return json(res, 200, { items: [], nextSyncToken: 'stub-sync-token' });
  }

  json(res, 404, { error: { code: 404, message: `stub has no route for ${path}` } });
});

server.listen(PORT, () => {
  console.log(`stub google listening on http://localhost:${PORT} with ${messages.size} messages`);
});

for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => {
    server.close();
    process.exit(0);
  });
}
