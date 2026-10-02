const CACHE_PREFIX = 'taradod-cache-';
const CACHE_NAME = `${CACHE_PREFIX}v2`;

self.addEventListener('install', event => {
    event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', event => {
    event.waitUntil((async () => {
        const cacheNames = await caches.keys();
        await Promise.all(
            cacheNames
                .filter(name => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME)
                .map(name => caches.delete(name))
        );
        await self.clients.claim();
    })());
});

// Never cache authenticated or other dynamic pages. If the network is offline,
// show a small fallback instead of returning a broken navigation response.
self.addEventListener('fetch', event => {
    const request = event.request;
    if (request.method !== 'GET' || request.mode !== 'navigate') return;

    const requestUrl = new URL(request.url);
    if (requestUrl.origin !== self.location.origin) return;

    event.respondWith(fetch(request).catch(() => new Response(
        '<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>عدم اتصال</title><body style="font-family:sans-serif;text-align:center;padding:3rem"><h1>اتصال اینترنت برقرار نیست</h1><p>برای استفاده از برنامه، اتصال را بررسی و صفحه را دوباره بارگذاری کنید.</p></body></html>',
        { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
    )));
});
