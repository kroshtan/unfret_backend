// The link checker's old service worker, replaced by one that removes itself. /check used to install as a web app,
// and a browser keeps a service worker it installed until a new version of it says otherwise; this is that version.
// It takes over at once, deletes any caches this site made (the old worker made none), unregisters itself and handles
// no requests, so a phone that had the app gets the plain /check page from the server from then on.

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      for (const name of await caches.keys()) await caches.delete(name);
      await self.registration.unregister();
    })(),
  );
});
