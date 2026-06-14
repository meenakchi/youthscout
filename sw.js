// YouthScout SG — Service Worker v1
// Handles: offline caching, push notifications, background sync


const CACHE_NAME   = 'youthscout-v1';
const STATIC_CACHE = 'youthscout-static-v1';


// Assets to cache on install
const PRECACHE = [
  '/',
  '/index.html',
  '/manifest.json',
  'https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Syne:wght@700;800&family=DM+Sans:ital,wght@0,300;0,400;0,500;1,400&display=swap',
];


// ── INSTALL ──────────────────────────────────────────────────
self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(STATIC_CACHE).then(cache => cache.addAll(PRECACHE))
  );
  self.skipWaiting();
});


// ── ACTIVATE ─────────────────────────────────────────────────
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys
          .filter(k => k !== CACHE_NAME && k !== STATIC_CACHE)
          .map(k => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});


// ── FETCH: network-first, fallback to cache ───────────────────
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);


  // Don't intercept Supabase or external API calls
  if (url.hostname.includes('supabase.co') ||
      url.hostname.includes('api.telegram') ||
      url.hostname.includes('fonts.gstatic')) {
    return;
  }


  // opportunities.json: network-first with cache fallback
  if (url.pathname.endsWith('opportunities.json')) {
    e.respondWith(
      fetch(e.request)
        .then(res => {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
          return res;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }


  // Everything else: cache-first with network fallback and error handling
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      
      return fetch(e.request)
        .then(response => {
          // Don't cache invalid responses
          if (!response || response.status !== 200 || response.type === 'error') {
            return response;
          }
          
          // Cache successful response
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(e.request, clone));
          return response;
        })
        .catch(error => {
          console.error('[SW] Fetch failed:', error);
          // Try to find a fallback in cache (e.g., offline page)
          return caches.match('/index.html');
        });
    })
  );
});


// ── PUSH NOTIFICATIONS ────────────────────────────────────────
self.addEventListener('push', (e) => {
  let data = {};
  try { data = e.data?.json() || {}; } catch { data = { title: 'YouthScout', body: e.data?.text() }; }


  const options = {
    body:    data.body    || 'New opportunities are live — check them out!',
    icon:    data.icon    || '/icons/icon-192.png',
    badge:   data.badge   || '/icons/badge-72.png',
    tag:     data.tag     || 'youthscout-update',
    renotify: true,
    data:    { url: data.url || '/' },
    actions: [
      { action: 'view',    title: 'View now' },
      { action: 'dismiss', title: 'Dismiss'  },
    ],
  };


  e.waitUntil(
    self.registration.showNotification(data.title || 'YouthScout SG ✦', options)
  );
});


// ── NOTIFICATION CLICK ────────────────────────────────────────
self.addEventListener('notificationclick', (e) => {
  e.notification.close();


  if (e.action === 'dismiss') return;


  const target = e.notification.data?.url || '/';
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      const existing = list.find(c => c.url === target && 'focus' in c);
      if (existing) return existing.focus();
      return clients.openWindow(target);
    })
  );
});


// ── BACKGROUND SYNC (for saved opps when offline) ─────────────
self.addEventListener('sync', (e) => {
  if (e.tag === 'sync-saved') {
    e.waitUntil(syncSaved());
  }
});


async function syncSaved() {
  // Placeholder — in production this would sync IndexedDB saves to Supabase
  console.log('[SW] Background sync: saved opportunities');
}