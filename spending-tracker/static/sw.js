const CACHE = 'spendtrack-v2';
const STATIC = ['/static/style.css?v=2', '/static/chart.min.js', '/static/manifest.json', '/static/icon-180.png', '/static/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(STATIC)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});

self.addEventListener('push', e => {
  let payload = { title: 'SpendTrack', body: 'Time to log your spending.', url: '/', tag: 'spendtrack' };
  if (e.data) {
    try { payload = Object.assign(payload, e.data.json()); }
    catch (err) { payload.body = e.data.text(); }
  }
  e.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: '/static/icon-180.png',
      badge: '/static/icon-180.png',
      tag: payload.tag,
      data: { url: payload.url },
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const client of list) {
        if ('focus' in client) return client.focus();
      }
      return self.clients.openWindow(target);
    })
  );
});
