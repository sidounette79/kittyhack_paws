// Service worker minimal - juste ce qu'il faut pour être "installable"
// (Chrome exige un vrai handler 'fetch' enregistré). Aucune mise en cache de
// /app-api/* ni des photos : c'est un outil de revue en temps réel, des
// données périmées seraient pires qu'une absence de cache.
const SHELL_CACHE = "paws-review-shell-v1";
const SHELL_FILES = ["/", "/style.css", "/app.js"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_FILES)),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k))),
    ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  const isShellFile = SHELL_FILES.includes(url.pathname);
  if (!isShellFile || event.request.method !== "GET") {
    return; // laisse passer en réseau direct - données vivantes (API, photos)
  }
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(SHELL_CACHE).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request)),
  );
});
