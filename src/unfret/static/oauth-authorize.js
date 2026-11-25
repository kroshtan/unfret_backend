// The MCP sign-in page (/oauth/authorize): "Continue" makes a pass for the connection. The proof of work every new
// pass needs (new-pass.js) starts as soon as the page opens, so Continue usually goes straight on; the server makes
// the pass within the same limits as POST /v1/keys.

import { solve } from "./new-pass.js";

const form = document.getElementById("create-form");
const button = document.getElementById("create-button");
const status = document.getElementById("create-status");
const challengeField = document.getElementById("create-challenge");
const solutionField = document.getElementById("create-solution");

async function work() {
  const response = await fetch("/v1/keys/challenge?developer=true", { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(String(response.status));
  const challenge = await response.json();
  const solution = await solve(challenge.challenge, challenge.bits);
  challengeField.value = challenge.challenge;
  solutionField.value = solution;
}

if (form && button) {
  let pending = work();
  let waiting = false;
  button.disabled = false;
  form.addEventListener("submit", async (event) => {
    if (solutionField.value) return; // done in the background: go on
    event.preventDefault();
    if (waiting) return;
    waiting = true;
    button.disabled = true;
    status.textContent = "One moment…";
    try {
      await pending;
      form.submit();
    } catch {
      pending = work();
      pending.catch(() => {});
      button.disabled = false;
      status.textContent = "That didn't work. Try again, or use a pass you already have.";
    } finally {
      waiting = false;
    }
  });
  pending.catch(() => {}); // a failure is shown when Continue is pressed
}
