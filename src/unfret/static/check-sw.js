// The link checker's service worker. It caches nothing and touches no other request: it exists so the app can be
// installed (and so appears in Android's share sheet), and to keep a shared link out of the request. A share opens
// /check?title=…&text=…&url=…; this worker answers that navigation itself with a redirect to /check#…, so the link
// stays in the browser (a fragment is never sent) and never reaches the server, a proxy or their logs.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.mode !== "navigate" || request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname !== "/check" || !url.search) return;
  event.respondWith(Response.redirect(`${url.origin}/check#${url.search.slice(1)}`, 303));
});
