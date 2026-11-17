// The welcome page the extension opens on install: if an affiliate's link brought this visitor (ref.js), hand the
// code to the extension, which made its own pass and tells the server with it (POST /v1/affiliates/attribute). The
// extension's content script on this page relays {type: "unfret-ref", code} and answers {type:
// "unfret-ref-received", status}; once it has answered, the code is forgotten here. Nothing else leaves the page.
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

if (typeof window !== "undefined") offerRef();
