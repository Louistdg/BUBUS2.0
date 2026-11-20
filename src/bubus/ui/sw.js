// Service worker BUBUS : rend l'appli installable et garde l'interface en cache.
// Les données (/api/...) ne sont jamais mises en cache : elles viennent toujours du serveur.
const CACHE = 'bubus-shell-v6';
const SHELL = ['/assets/desk.css', '/assets/desk.js', '/assets/icon-192.png?v=crypto', '/assets/icon-512.png?v=crypto', '/assets/icon-maskable-512.png?v=crypto'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).catch(() => undefined));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))));
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname === '/login') return;
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => new Response(
      '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
      '<body style="font-family:system-ui;background:#0f1716;color:#eef4f2;padding:24px">' +
      '<h1>BUBUS hors ligne</h1><p>Pas de connexion Internet. Réessaie dès que le réseau revient.</p></body>',
      {headers: {'Content-Type': 'text/html; charset=utf-8'}})));
    return;
  }
  if (SHELL.includes(url.pathname + url.search)) {
    event.respondWith(fetch(request).then(response => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE).then(cache => cache.put(request, copy));
      }
      return response;
    }).catch(() => caches.match(request)));
  }
});
