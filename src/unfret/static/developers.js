// /developers: the Copy buttons next to the code blocks (code-copy.js), and "Make a developer pass" (a pass with
// free deep checks, once per browser: new-pass.js does the proof of work, and the server allows one a day per
// network). The prices, limits, sample response and times are put in by the server.

import { copy } from "./code-copy.js";
import { makePass } from "./new-pass.js";
import { api } from "./wallet.js";

const MADE = "unfret-developer-pass"; // set once this browser made one (the date): one per browser

function copyButton(text) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "copy";
  button.textContent = "Copy";
  button.addEventListener("click", () => copy(button, text));
  return button;
}

function secret(label, hint, value) {
  const head = document.createElement("p");
  head.className = "secret-label";
  const strong = document.createElement("strong");
  strong.textContent = label;
  const small = document.createElement("span");
  small.className = "muted small";
  small.textContent = hint;
  head.append(strong, small);
  const row = document.createElement("div");
  row.className = "secret-value";
  const code = document.createElement("code");
  code.textContent = value;
  row.append(code, copyButton(value));
  return [head, row];
}

function made() {
  try {
    return localStorage.getItem(MADE);
  } catch {
    return null;
  }
}

const make = document.getElementById("dev-pass-make");
const status = document.getElementById("dev-pass-status");
const result = document.getElementById("dev-pass-result");

if (make && made()) {
  make.hidden = true;
  status.textContent = `This browser made a developer pass on ${made()}. Lost it? Its backup code gets it back at My checks.`;
}

make?.addEventListener("click", async () => {
  make.disabled = true;
  status.textContent = "Making your pass: a few seconds of work in this browser…";
  try {
    const issued = await makePass(api, { developer: true });
    try {
      localStorage.setItem(MADE, new Date().toISOString().slice(0, 10));
    } catch {
      // private window: the server's limit per network still holds
    }
    make.hidden = true;
    status.textContent = issued.free_checks
      ? `Your developer pass, with ${issued.free_checks} free deep checks. Save both now: they're shown only this once.`
      : "Your pass is made, but without free checks: this network already had its developer pass today. " +
        "Save both now; buy checks for it, or use quick checks (free).";
    result.replaceChildren(
      ...secret("Your pass", "Send it as Authorization: Bearer <pass>, or set UNFRET_PASS.", issued.key),
      ...secret("Its backup code", "Gets you a new pass with the same checks if you lose this one.", issued.recovery_code),
    );
    result.hidden = false;
  } catch (error) {
    make.disabled = false;
    status.textContent = error.status === 429
      ? "Too many new passes from here today; try again tomorrow."
      : `Couldn't make a pass: ${error.message}`;
  }
});
