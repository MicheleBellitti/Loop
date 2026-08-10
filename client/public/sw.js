/**
 * The service worker.
 *
 * Two jobs, both named in the handoff as states the prototypes only imply:
 * offline read-only mode, and web push.
 *
 * The cache is stale-while-revalidate for GETs under /api, so an installed PWA
 * on a train shows the pipeline it last saw, with a header the client can use
 * to say so. Mutations are never cached and never queued — a correction that
 * silently applied three hours later would be worse than one that failed.
 */

const CACHE = 'loop-v2';
const SHELL = ['/', '/index.html', '/manifest.webmanifest'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // The SSE stream must never be cached or replayed.
  if (url.pathname === '/api/stream') return;

  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          void caches.open(CACHE).then((cache) => cache.put(request, copy));
          return response;
        })
        .catch(async () => {
          const cached = await caches.match(request);
          if (!cached) throw new Error('offline and nothing cached');
          // The client reads this to render read-only mode honestly.
          const headers = new Headers(cached.headers);
          headers.set('x-loop-offline', '1');
          return new Response(await cached.blob(), { status: 200, headers });
        }),
    );
    return;
  }

  // Navigations are network-first.
  //
  // Cache-first on the shell is the usual PWA recipe and it is a trap: the
  // shell names hashed asset files, so a cached index.html keeps asking for a
  // bundle that no longer exists and the app renders nothing at all — with a
  // MIME-type console error that names neither the file nor the cause. Ask the
  // network, fall back to the cache when there is no network. That is what
  // "offline read-only mode" actually requires.
  if (request.mode === 'navigate' || (request.destination === 'document')) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          void caches.open(CACHE).then((cache) => cache.put('/index.html', copy));
          return response;
        })
        .catch(async () => (await caches.match('/index.html')) ?? Response.error()),
    );
    return;
  }

  // Hashed assets are immutable, so cache-first is correct for them and only
  // for them.
  event.respondWith(
    caches.match(request).then(
      (cached) =>
        cached ??
        fetch(request).then((response) => {
          if (response.ok) {
            const copy = response.clone();
            void caches.open(CACHE).then((cache) => cache.put(request, copy));
          }
          return response;
        }),
    ),
  );
});

self.addEventListener('push', (event) => {
  if (!event.data) return;
  const payload = event.data.json();
  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      // One notification per thing, ever: the tag collapses a repeat rather
      // than stacking a second buzz for the same suggestion.
      tag: payload.tag,
      renotify: false,
      data: { url: payload.url },
      icon: '/icon.svg',
      badge: '/icon.svg',
    }),
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = event.notification.data?.url ?? '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windows) => {
      for (const client of windows) {
        if ('focus' in client) return client.focus();
      }
      return self.clients.openWindow(target);
    }),
  );
});
