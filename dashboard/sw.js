// Grand Central — offline service worker (E45, Tier 1: read-only offline).
//
// Strategy: network-first for our own origin (the board is live data — always
// prefer fresh, fall back to the last cached copy when the network is gone),
// cache-first for cross-origin assets (Google Fonts) so the page still looks
// right offline. Only GETs are touched — POST actions are never cached or
// queued, so nothing side-effectful can fire from a stale tab. Prime the cache
// by loading the app once online; after that it opens with no network.

const VERSION = 'gc-cache-v9';   // E265: Ask cards

// Precached on install so the very first offline open works even for paths the
// user hasn't visited yet. Everything else is cached on demand as they browse.
const SHELL = [
  '/', '/index.html', '/settings.html',
  '/favicon.svg', '/manifest.json', '/grand_central.json', '/release_notes.json',
];

// Cache key ignores the ?t=... cache-buster the board appends, so a poll and
// an offline fallback resolve to the same stored entry.
function keyFor(req) {
  const u = new URL(req.url);
  u.search = '';
  return u.toString();
}

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(VERSION)
      .then((c) => Promise.allSettled(SHELL.map((u) => c.add(u))))
      .then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;   // read-only offline: never touch actions
  const url = new URL(req.url);

  if (url.origin === self.location.origin) {
    // Our board/data: network-first, fall back to the last cached copy.
    e.respondWith(
      fetch(req)
        .then((resp) => {
          if (resp && resp.ok) {
            const copy = resp.clone();
            caches.open(VERSION).then((c) => c.put(keyFor(req), copy)).catch(() => {});
          }
          return resp;
        })
        .catch(() => caches.match(keyFor(req)).then((hit) => hit || caches.match(req))));
    return;
  }

  // Cross-origin (fonts): cache-first so the UI survives offline.
  e.respondWith(
    caches.match(req).then((hit) => hit || fetch(req).then((resp) => {
      const copy = resp.clone();
      caches.open(VERSION).then((c) => c.put(req, copy)).catch(() => {});
      return resp;
    }).catch(() => hit)));
});

/* ---------------------------------------------------------------------------
   E188 — Web Push. The host signs a payload with its VAPID key and hands it
   to the browser vendor's push service; this runs when it arrives, whether
   or not the board is open. Clicking focuses an open window if there is one.
   --------------------------------------------------------------------------- */
self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { body: e.data && e.data.text() }; }
  const title = d.title || 'Grand Central';
  e.waitUntil(self.registration.showNotification(title, {
    body: d.body || '',
    tag: d.tag || 'gc',
    renotify: false,
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    data: { url: d.url || '/' },
  }));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil((async () => {
    const wins = await clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const w of wins) {
      if (w.url.includes(self.location.origin)) { await w.focus(); if ('navigate' in w) await w.navigate(url); return; }
    }
    await clients.openWindow(url);
  })());
});
