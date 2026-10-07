// Service Worker for online-only PWA with offline notification

// Version your cache to force updates
const CACHE_VERSION = '4';
const CACHE_NAME = 'offline-only-v' + CACHE_VERSION;

// The offline fallback page
const OFFLINE_PAGE = './offline.html';

// Install event - cache the offline page
self.addEventListener('install', event => {
  console.log('[Service Worker] Installing');
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      console.log('[Service Worker] Caching offline page');
      // Use no-cache to force revalidation
      return cache.add(new Request(OFFLINE_PAGE, { cache: 'no-cache' }));
    })
  );
  // Activate immediately
  self.skipWaiting();
});

// Activate event - clean up any old caches
self.addEventListener('activate', event => {
  console.log('[Service Worker] Activating');
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.filter(cacheName => {
          return cacheName.startsWith('offline-only-') && cacheName !== CACHE_NAME;
        }).map(cacheName => {
          console.log('[Service Worker] Removing old cache', cacheName);
          return caches.delete(cacheName);
        })
      );
    }).then(() => {
      // Update the offline page whenever the service worker activates
      return caches.open(CACHE_NAME).then(cache => {
        console.log('[Service Worker] Re-caching offline page on activation');
        return cache.add(new Request(OFFLINE_PAGE, { cache: 'reload' }));
      });
    })
  );
  // Enable navigation preload to improve first-load when online
  if (self.registration && 'navigationPreload' in self.registration) {
    try {
      self.registration.navigationPreload.enable();
    } catch (e) {
      // ignore
    }
  }
  // Take control of all clients
  return self.clients.claim();
});

// Fetch event: network-first for navigation with offline fallback; network-only for others
self.addEventListener('fetch', event => {
  // Only handle GET requests
  if (event.request.method !== 'GET') return;

  const isNavigationRequest = event.request.mode === 'navigate';

  event.respondWith((async () => {
    if (isNavigationRequest) {
      try {
        // Use navigation preload if available for faster responses
        const preload = event.preloadResponse ? await event.preloadResponse : null;
        const response = preload || await fetch(event.request);

        // If server error 5xx, fall back to offline page
        if (!response.ok && response.status >= 500 && response.status < 600) {
          console.log('[Service Worker] Server error', response.status);
          const cached = await caches.match(OFFLINE_PAGE);
          return cached || response;
        }
        return response;
      } catch (err) {
        console.log('[Service Worker] Navigation fetch failed, serving offline page');
        const cached = await caches.match(OFFLINE_PAGE);
        if (cached) return cached;
        return new Response('Offline', { status: 503, headers: { 'Content-Type': 'text/plain' } });
      }
    }

    // Non-navigation requests: let network fail if offline
    try {
      return await fetch(event.request);
    } catch {
      return new Response('Network error', { status: 503, headers: { 'Content-Type': 'text/plain' } });
    }
  })());
});

// Optional: allow immediate activation via postMessage
self.addEventListener('message', event => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

// --- Push notifications (cat entry/exit alerts) ---
self.addEventListener('push', event => {
  let payload = { title: 'Kittyhack', body: '', url: '/', tag: 'kittyhack' };
  try {
    if (event.data) payload = Object.assign(payload, event.data.json());
  } catch (e) {
    console.warn('[Service Worker] Push payload was not JSON:', e);
  }

  const options = {
    body: payload.body,
    tag: payload.tag,
    // 06.10, Sid ("6 passages, 1 seule notif reçue"): tags are fixed per
    // event TYPE (kittyhack-inside/-outside/-glance), not per event - so a
    // 2nd "est sorti" while the 1st is still showing silently REPLACES it
    // in the tray instead of stacking, and without renotify the browser
    // never re-alerts (no sound/vibration/banner) on that replace - real,
    // standard Notifications API behavior, confirmed by testing a manual
    // push (delivered fine) right after 6 real passages mostly went
    // unnoticed. renotify forces a fresh alert on every single push, even
    // when it reuses an existing tag.
    renotify: true,
    data: { url: payload.url },
    icon: '/favicon-192x192.png',
    // Android's status-bar icon is alpha-mask only (color is ignored and
    // renders as a solid blob) - needs a real white-on-transparent silhouette,
    // not the full-color logo used for `icon` above.
    badge: '/notif-badge.png',
  };
  if (payload.image) {
    options.image = payload.image;
  }

  event.waitUntil(self.registration.showNotification(payload.title, options));
});

// 06.10, Sid ("notifs qui s'arrêtent après un moment, je pense que c'est un
// problème de connexion pas pérenne"): investigated a real gap - 3 real cat
// passages (20:47/22:14/23:08) got zero notification on her phone, even
// though the server's webpush() call raised no error for any of them and
// the stored subscription (webpush_subscriptions.json) hadn't changed since
// the day before. That combination - server thinks it succeeded, same old
// subscription never updated - matches a subscription the browser silently
// invalidated/rotated in the background. The Push API has a dedicated event
// for exactly this ("a push subscription has been invalidated, or is about
// to be"), which this service worker never listened for - so the app had no
// way to notice and re-subscribe on its own. Mirrors enableKittyhackNotifications()
// in webpush-client.js (can't share code directly - different script scope).
function _urlBase64ToUint8ArraySW(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}

self.addEventListener('pushsubscriptionchange', event => {
  console.log('[Service Worker] Push subscription changed/expired - re-subscribing');
  event.waitUntil(
    (async () => {
      try {
        const keyResp = await fetch('/webpush/vapid-public-key');
        const { publicKey } = await keyResp.json();
        const newSubscription = await self.registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: _urlBase64ToUint8ArraySW(publicKey),
        });
        await fetch('/webpush/subscribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(newSubscription),
        });
        console.log('[Service Worker] Re-subscribed to push successfully');
      } catch (e) {
        console.warn('[Service Worker] Failed to re-subscribe after subscription change:', e);
      }
    })()
  );
});

// Clicking the notification focuses/opens the app instead of leaving it in the tray.
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      for (const client of clientList) {
        if ('focus' in client) return client.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(targetUrl);
    })
  );
});