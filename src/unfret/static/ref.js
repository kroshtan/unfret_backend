// An affiliate's link (unfret.net/?ref=CODE, on any page) brings a visitor: the code is kept in this browser's own
// storage (localStorage on this site: no cookie, no request, nothing to a third party) until a pass exists, then sent
// with it once (new-pass.js; the extension's pass through /welcome), so that pass counts for the affiliate. The first
// code is kept, for 30 days; the address bar loses the ?ref= at once. Checkout fills its code field with it.

const KEY = "unfret-ref";
const CODE_RE = /^[A-Za-z0-9]{3,24}$/;
const MAX_AGE_MS = 30 * 24 * 3600 * 1000;

function read(storage) {
  try {
    const kept = JSON.parse(storage.getItem(KEY) ?? "null");
    if (!kept || !CODE_RE.test(kept.code ?? "") || !(Date.now() - kept.at < MAX_AGE_MS)) return null;
    return kept;
  } catch {
    return null; // storage off (a private window): no affiliate, nothing breaks
  }
}

/** The kept affiliate code (upper case), or null. */
export function storedRef(storage = globalThis.localStorage) {
  return storage ? (read(storage)?.code ?? null) : null;
}

/** Forget the code: a pass has it now (or it can't be used). */
export function clearRef(storage = globalThis.localStorage) {
  try {
    storage?.removeItem(KEY);
  } catch {
    // nothing kept, nothing to clear
  }
}

/** Keep a link's ?ref= (the first one wins) and take it out of the address bar. Returns the kept code or null. */
export function captureRef(win = globalThis.window, storage = globalThis.localStorage) {
  if (!win?.location) return null;
  const url = new URL(win.location.href);
  const code = (url.searchParams.get("ref") ?? "").trim();
  if (!url.searchParams.has("ref")) return storedRef(storage);
  if (CODE_RE.test(code) && !read(storage)) {
    try {
      storage.setItem(KEY, JSON.stringify({ code: code.toUpperCase(), at: Date.now() }));
    } catch {
      // storage off: the link still works, it just isn't remembered
    }
  }
  url.searchParams.delete("ref");
  win.history.replaceState(win.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  return storedRef(storage);
}

if (typeof window !== "undefined") captureRef();
