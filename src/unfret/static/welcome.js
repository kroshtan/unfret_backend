// The welcome page the extension opens on install.
//
// /welcome?setup=1 (extension 0.12.0, on install or from its "Get your free checks"): the extension makes its pass only
// once it knows whether this browser already had its free checks. A reinstall wipes the extension's own note of that
// when the browser doesn't sync, so the page tells it what the link checker's note says (FREE_GIVEN: a yes or no,
// nothing else): {type: "unfret-free-ask", had}. The extension answers {type: "unfret-free-answer", status, free},
// and the page moves on to the welcome that fits. A pass made with free checks, or without them because this browser
// had them, sets the note here too, so the link checker doesn't give a second round either. With `renewed=1` (the
// extension's old pass was retired: the 0.13.0 fresh start) the welcome it moves on to says so in one line.
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

/**
 * The welcome page for the extension's answer: with the free checks, why there were none, or the plain one. After a
 * retired pass (`renewed`: the extension opened /welcome?setup=1&renewed=1), it keeps saying so, in one line.
 */
export function nextWelcome(answer, renewed = false) {
  const free = Number(answer?.free);
  const path =
    answer?.status === "free" && Number.isInteger(free) && free > 0
      ? `/welcome?free=${free}`
      : answer?.status === "had-free" || answer?.status === "none-today"
        ? `/welcome?note=${answer.status}`
        : "/welcome";
  if (renewed !== true) return path;
  return `${path}${path.includes("?") ? "&" : "?"}renewed=1`;
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
  const renewed = new URLSearchParams(win.location.search).get("renewed") === "1";
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
    go(nextWelcome(event.data, renewed));
  });
  let left = tries;
  const ask = () => {
    if (done || left-- <= 0) return;
    win.postMessage({ type: FREE_ASK, had }, win.location.origin);
    setTimeout(ask, everyMs);
  };
  ask();
  setTimeout(() => go(nextWelcome(null, renewed)), giveUpMs);
  return had;
}

if (typeof window !== "undefined") {
  if (new URLSearchParams(window.location.search).get("setup") === "1") askFreeChecks();
}
