const CACHE = 'agentbank-static-m4-light-v4';
const SHELL = ['/app/', '/app/index.html', '/app/styles.css', '/app/app.js', '/app/presentation.js', '/app/manifest.json', '/app/icon-192.png', '/app/icon-512.png', '/app/webim.html', '/app/webim.js', '/app/plan.html'];
self.addEventListener('install', event => { event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL))); self.skipWaiting(); });
self.addEventListener('activate', event => { event.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  // Never intercept API, authenticated requests, query-bearing URLs, or runtime data.
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.search || event.request.headers.has('Authorization') || !SHELL.includes(url.pathname)) return;
  event.respondWith(fetch(event.request).catch(() => caches.open(CACHE).then(cache => cache.match(url.pathname))));
});
