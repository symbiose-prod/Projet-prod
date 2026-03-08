/**
 * Service Worker — Ferment Station PWA
 *
 * Stratégie : Network-first (l'app est online-only),
 * avec cache des assets statiques pour un chargement rapide
 * et une page offline de secours.
 */

const CACHE_NAME = "ferment-v1";

// Assets à pré-cacher à l'installation
const PRECACHE_URLS = [
  "/static/offline.html",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/manifest.json",
];

// ─── Installation : pré-cache des assets critiques ───────────────────────────

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

// ─── Activation : nettoyage des anciens caches ──────────────────────────────

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== CACHE_NAME)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

// ─── Fetch : network-first, fallback cache, puis offline page ────────────────

self.addEventListener("fetch", (event) => {
  const { request } = event;

  // Ignorer les requêtes non-GET (POST EasyBeer, etc.)
  if (request.method !== "GET") return;

  // Ignorer les WebSockets (NiceGUI en a besoin)
  if (request.url.includes("ws://") || request.url.includes("wss://")) return;

  event.respondWith(
    fetch(request)
      .then((response) => {
        // Mettre en cache les assets statiques au passage
        if (
          response.ok &&
          (request.url.includes("/static/") ||
            request.url.includes("/_nicegui/") ||
            request.url.includes("fonts.googleapis.com") ||
            request.url.includes("fonts.gstatic.com"))
        ) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
        }
        return response;
      })
      .catch(() => {
        // Réseau indisponible → essayer le cache
        return caches.match(request).then((cached) => {
          if (cached) return cached;

          // Pour les navigations (pages HTML), afficher la page offline
          if (request.mode === "navigate") {
            return caches.match("/static/offline.html");
          }

          // Sinon, laisser échouer normalement
          return new Response("Offline", {
            status: 503,
            statusText: "Service Unavailable",
          });
        });
      })
  );
});
