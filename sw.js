// YouthScout SG — Service Worker v3 (FIXED)
// FIXED: network-first for HTML/JS/CSS so updates actually show up
// FIXED: versioned cache name so old cache is always busted on deploy
// FIXED: chrome-extension scheme check to prevent cache errors
// FIXED: deprecated apple-mobile-web-app-capable warning

const CACHE_VERSION = 'v3';
const CACHE_NAME    = `youthscout-${CACHE_VERSION}`;


// Only pre-cache fonts (stable, slow to fetch)
const PRECACHE = [
  'https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Syne:wght@700;800&family=DM+Sans:ital,wght@0,300;0,400;0,500;1,400&display=swap',
];


// ── INSTALL ──────────────────────────────────────────────────
self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())  // activate immediately
  );
});


// ── ACTIVATE: delete ALL old caches ──────────────────────────
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => {
          console.log('[SW] Deleting old cache:', k);
          return caches.delete(k);
        })
      ))
      .then(() => self.clients.claim())
  );
});


// ── FETCH ────────────────────────────────────────────────────
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // NEVER intercept: chrome-extension, Supabase, Telegram, Google Fonts data, external APIs
  const bypass = [
    'supabase.co', 'api.telegram', 'fonts.gstatic.com',
    'googleapis.com', 'cdn.jsdelivr.net',
  ];
  
  // Skip chrome-extension scheme (prevents cache error)
  if (url.protocol === 'chrome-extension:') return;
  
  // Skip bypass hosts
  if (bypass.some(h => url.hostname.includes(h))) return;


  // opportunities.json → network-first, short cache
  if (url.pathname.endsWith('opportunities.json')) {
    e.respondWith(
      fetch(e.request, { cache: 'no-store' })
        .then(res => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
          }
          return res;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }


  // index.html, JS, CSS → ALWAYS network-first, never serve stale app shell
  if (
    url.pathname === '/' ||
    url.pathname.endsWith('.html') ||
    url.pathname.endsWith('.js') ||
    url.pathname.endsWith('.css')
  ) {
    e.respondWith(
      fetch(e.request, { cache: 'no-store' })
        .then(res => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
          }
          return res;
        })
        .catch(() => caches.match(e.request))  // offline fallback only
    );
    return;
  }


  // Everything else (icons, fonts already cached) → cache-first
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request).then(res => {
        if (res && res.status === 200) {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
        }
        return res;
      }).catch(() => caches.match('/index.html'));
    })
  );
});


// ── MESSAGE: force cache clear (called from app on sign-out) ──
self.addEventListener('message', (e) => {
  if (e.data === 'CLEAR_CACHE') {
    caches.keys().then(keys => Promise.all(keys.map(k => caches.delete(k))));
  }
  if (e.data === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});


// ── PUSH NOTIFICATIONS ────────────────────────────────────────
self.addEventListener('push', (e) => {
  let data = {};
  try { data = e.data?.json() || {}; } catch { data = { title: 'YouthScout', body: e.data?.text() }; }

  // Use absolute paths or verify icons exist at these locations
  const options = {
    body:       data.body    || 'New opportunities are live — check them out!',
    icon:       data.icon    || '/icons/icon-192.png',
    badge:      data.badge   || '/icons/badge-72.png',
    tag:        data.tag     || 'youthscout-update',
    renotify:   true,
    data:       { url: data.url || '/' },
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


// ── BACKGROUND SYNC ───────────────────────────────────────────
self.addEventListener('sync', (e) => {
  if (e.tag === 'sync-saved') {
    e.waitUntil(syncSaved());
  }
});


async function syncSaved() {
  console.log('[SW] Background sync: saved opportunities');
}