// Change to v2 to force the browser to update the worker!
const CACHE_NAME = 'searing-pos-v2';
const OFFLINE_URL = '/';

const ASSETS_TO_CACHE = [
    '/',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(ASSETS_TO_CACHE);
        })
    );
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames.map((cache) => {
                    // Delete the old v1 cache
                    if (cache !== CACHE_NAME) {
                        return caches.delete(cache);
                    }
                })
            );
        })
    );
    self.clients.claim();
});

self.addEventListener('fetch', (event) => {
    // 1. Ignore POST requests
    if (event.request.method !== 'GET') return;

    // 2. CRITICAL FIX: Ignore Cross-Origin requests (Bootstrap CDNs, Google Fonts)
    // Let the browser fetch them natively!
    if (!event.request.url.startsWith(self.location.origin)) return;

    // 3. Ignore API and Admin routes just to be safe
    if (event.request.url.includes('/api/') || event.request.url.includes('/admin/')) return;

    // For everything else, try the network first, then fall back to cache
    event.respondWith(
        fetch(event.request).catch(() => {
            return caches.match(event.request).then((response) => {
                if (response) {
                    return response;
                }
                // If offline and request is for a webpage, return the cached root
                if (event.request.mode === 'navigate') {
                    return caches.match(OFFLINE_URL);
                }
            });
        })
    );
});