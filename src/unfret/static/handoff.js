// Handing a new API key to the Unfret browser extension, so nobody has to copy and paste it.
//
// The page posts {type: "unfret-key", key} to itself. The extension's content script (injected only on this site's
// /checkout pages) accepts it only from this very window and origin, stores the key as its sign-in and answers with
// {type: "unfret-key-received", status}: "signed-in", "already" (it uses this key), "other-account" (it is signed in
// with another key or wallet and changes nothing until the page asks again with replace: true) or "invalid". From
// 0.12.0, "other-account" comes with free_pass: true when what the extension has is the free pass it made itself:
// on replace, it moves that pass's free checks to the new one (here: "free-pass"). Also from 0.12.0: "needs-click"
// (the extension has no pass yet and takes one only right after a click on this page, or in a tab it opened itself:
// any site can open this page; nothing was kept, so the page offers a button and hands the key over again on its
// click) and "confirm" (on replace, the extension asks in its own window; its answer follows as "signed-in" or "kept",
// see awaitConfirmation). The key only goes from this page to the extension; the answer carries no key, account or
// balance. No answer within ACK_WAIT_MS: no (recent enough) extension in this browser.
//
// Also here: the key ID a "Top up" link names (/checkout?mode=topup&key_id=<10 letters>), the public part of a key.

export const KEY_MESSAGE = "unfret-key";
export const KEY_RECEIVED = "unfret-key-received";
export const ACK_WAIT_MS = 2000;
const STATUSES = new Set(["signed-in", "already", "other-account", "invalid", "needs-click", "confirm", "kept"]);
const KEY_RE = /^ufk_[a-z2-7]{10}_[a-z2-7]{52}$/;
const KEY_ID_RE = /^[a-z2-7]{10}$/;

/**
 * Offer `key` to the extension and wait for its answer.
 *
 * @param {string} key - A whole API key (ufk_…).
 * @param {{replace?: boolean, win?: Window, waitMs?: number}} [options] - replace: the user agreed to swap accounts.
 * @returns {Promise<string>} The extension's status ("free-pass" for "other-account" with free_pass: true), or
 *   "no-extension" when nothing answered in time.
 */
export function handOffKey(key, { replace = false, win = window, waitMs = ACK_WAIT_MS } = {}) {
  if (!KEY_RE.test(key)) return Promise.resolve("invalid");
  return new Promise((resolve) => {
    const origin = win.location.origin;
    const done = (status) => {
      win.removeEventListener("message", listen);
      clearTimeout(timer);
      resolve(status);
    };
    const listen = (event) => {
      if (event.source !== win || event.origin !== origin) return;
      const data = event.data;
      if (data?.type !== KEY_RECEIVED || !STATUSES.has(data.status)) return;
      done(data.status === "other-account" && data.free_pass === true ? "free-pass" : data.status);
    };
    const timer = setTimeout(() => done("no-extension"), waitMs);
    win.addEventListener("message", listen);
    win.postMessage(replace ? { type: KEY_MESSAGE, key, replace: true } : { type: KEY_MESSAGE, key }, origin);
  });
}

/**
 * After "confirm": wait for the user's answer in the extension's own window, "signed-in" or "kept" (closed unanswered
 * counts as "kept"). Calls `then` once.
 *
 * @param {(status: string) => void} then - Called with the answer.
 * @param {{win?: Window}} [options] - The page's window.
 * @returns {() => void} Stops waiting.
 */
export function awaitConfirmation(then, { win = window } = {}) {
  const origin = win.location.origin;
  const listen = (event) => {
    if (event.source !== win || event.origin !== origin) return;
    const status = event.data?.type === KEY_RECEIVED ? event.data.status : null;
    if (status !== "signed-in" && status !== "kept") return;
    win.removeEventListener("message", listen);
    then(status);
  };
  win.addEventListener("message", listen);
  return () => win.removeEventListener("message", listen);
}

/**
 * The key ID a link asks to top up (`key_id`), or null when it isn't one.
 *
 * @param {URLSearchParams} params - The page's query.
 * @returns {string | null} The 10-letter key ID.
 */
export function topupKeyId(params) {
  const value = (params.get("key_id") ?? "").trim().toLowerCase();
  return KEY_ID_RE.test(value) ? value : null;
}
