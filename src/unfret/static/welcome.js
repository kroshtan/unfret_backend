// The welcome page the extension opens on install: if an affiliate's link brought this visitor (ref.js), hand the
// code to the extension, which made its own pass and tells the server with it (POST /v1/affiliates/attribute). The
// extension's content script on this page relays {type: "unfret-ref", code} and answers {type:
// "unfret-ref-received", status}; once it has answered, the code is forgotten here. Nothing else leaves the page.
//
// /welcome?setup=1 (extension 0.12.0, on install or from its "Get your free checks"): the extension makes its pass only
// once it knows whether this browser already had its free checks. A reinstall wipes the extension's own note of that
// when the browser doesn't sync, so the page tells it what the link checker's note says (FREE_GIVEN: a yes or no,
// nothing else): {type: "unfret-free-ask", had}. The extension answers {type: "unfret-free-answer", status, free},
// and the page moves on to the welcome that fits. A pass made with free checks, or without them because this browser
// had them, sets the note here too, so the link checker doesn't give a second round either.
import { clearRef, storedRef } from "./ref.js";

export const REF_MESSAGE = "unfret-ref";
export const REF_RECEIVED = "unfret-ref-received";
const DONE = new Set(["attributed", "already", "not_eligible", "invalid"]);

/** Offer the kept code to the extension a few times (its content script may start after this page). */
export function offerRef(win = window, { tries = 5, everyMs = 1000 } = {}) {
  const code = storedRef();
  if (!code) return null;
  let answered = false;
  win.addEventListener("message", (event) => {
    if (event.source !== win || event.origin !== win.location.origin) return;
    if (event.data?.type !== REF_RECEIVED || !DONE.has(event.data.status)) return;
    answered = true;
    clearRef();
  });
  let left = tries;
  const offer = () => {
    if (answered || left-- <= 0) return;
    win.postMessage({ type: REF_MESSAGE, code }, win.location.origin);
    setTimeout(offer, everyMs);
  };
  offer();
  return code;
}

export const FREE_ASK = "unfret-free-ask";
export const FREE_ANSWER = "unfret-free-answer";
const FREE_GIVEN = "unfret-free-pass"; // the link checker's note (check.js): this browser had its free pass

function hadFreeChecks(storage) {
  try {
    return storage.getItem(FREE_GIVEN) !== null;
  } catch {
    return false; // no storage: the extension's own note and the server's limits decide
  }
}

function markFreeChecks(storage) {
  try {
    storage.setItem(FREE_GIVEN, "1");
  } catch {
    // not remembered
  }
}

/** The welcome page for the extension's answer: with the free checks, why there were none, or the plain one. */
export function nextWelcome(answer) {
  const free = Number(answer?.free);
  if (answer?.status === "free" && Number.isInteger(free) && free > 0) return `/welcome?free=${free}`;
  if (answer?.status === "had-free" || answer?.status === "none-today") return `/welcome?note=${answer.status}`;
  return "/welcome";
}

/**
 * On /welcome?setup=1: tell the extension whether this browser had its free checks, a few times (its content script
 * may start after this page), and move on once it answers; without an answer (no extension, an older one), the plain
 * welcome page.
 */
export function askFreeChecks(
  win = window,
  storage = globalThis.localStorage,
  { tries = 20, everyMs = 500, giveUpMs = 60_000 } = {},
) {
  const had = hadFreeChecks(storage);
  let done = false;
  const go = (path) => {
    if (done) return;
    done = true;
    win.location.replace(path);
  };
  win.addEventListener("message", (event) => {
    if (event.source !== win || event.origin !== win.location.origin) return;
    if (event.data?.type !== FREE_ANSWER) return;
    if (["free", "had-free", "none-today"].includes(event.data.status)) markFreeChecks(storage);
    go(nextWelcome(event.data));
  });
  let left = tries;
  const ask = () => {
    if (done || left-- <= 0) return;
    win.postMessage({ type: FREE_ASK, had }, win.location.origin);
    setTimeout(ask, everyMs);
  };
  ask();
  setTimeout(() => go("/welcome"), giveUpMs);
  return had;
}

if (typeof window !== "undefined") {
  if (new URLSearchParams(window.location.search).get("setup") === "1") askFreeChecks();
  else offerRef();
}
