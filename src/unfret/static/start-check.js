// A link pasted on the home page (or /nl) is checked on the link checker (/check) at once, as if Check was tapped
// there: with the visitor's pass, or the browser's free checks, or else a free quick check. This page keeps the link
// in the tab's session storage for the moment it takes to open /check, and check.js takes it out and starts the
// check. Session storage rather than the address: only this site's own pages can put a link there, so no other site
// can send a visitor to /check and spend one of their checks; and the link never reaches the address bar, the
// history or a server log. Without session storage (a locked-down browser), /check opens with the link filled in,
// and the visitor taps Check.
import { extractLink } from "./check-core.js";

const KEY = "unfret-start-check";
const MAX_AGE_MS = 60 * 1000;

/** Keep `link` for /check in this tab. Returns false when the browser keeps nothing (the link goes in the fragment). */
export function handOff(link, storage = globalThis.sessionStorage) {
  try {
    storage.setItem(KEY, JSON.stringify({ link, at: Date.now() }));
    return true;
  } catch {
    return false;
  }
}

/** The link this tab's previous page handed over (once: it is removed), or "" when there is none or it is stale. */
export function takeHandedLink(storage = globalThis.sessionStorage) {
  try {
    const kept = JSON.parse(storage.getItem(KEY) ?? "null");
    storage.removeItem(KEY);
    if (!kept || !(Date.now() - kept.at < MAX_AGE_MS)) return "";
    return extractLink(kept.link);
  } catch {
    return "";
  }
}

/**
 * Wire a paste-a-link form: its text field, an error line, and buttons with `data-example` that fill in a link.
 * On submit, a link goes to /check (handed over as above); anything else gets a short error, in `messages`' words.
 *
 * @param {HTMLFormElement} form - With an `input` field and an element `[data-error]` for the error.
 * @param {{empty: string, invalid: string}} messages - What the error line says for no text, and for text without a link.
 * @param {Window} win - The window to move on from (tests pass their own).
 */
export function wireStartForm(form, messages, win = globalThis.window) {
  if (!form) return;
  const field = form.querySelector("input");
  const error = form.querySelector("[data-error]");
  const say = (text) => {
    error.textContent = text;
    error.hidden = !text;
    if (text) field.setAttribute("aria-invalid", "true");
    else field.removeAttribute("aria-invalid");
  };
  field.addEventListener("input", () => say(""));
  for (const example of form.querySelectorAll("[data-example]")) {
    example.addEventListener("click", () => {
      field.value = new URL(example.dataset.example, win.location.href).href;
      say("");
      field.focus();
    });
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const link = extractLink(field.value);
    if (!link) {
      say(field.value.trim() ? messages.invalid : messages.empty);
      field.focus();
      return;
    }
    win.location.assign(handOff(link) ? "/check" : `/check#url=${encodeURIComponent(link)}`);
  });
}
