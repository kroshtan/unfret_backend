// The MCP sign-in page (/oauth/authorize): "Make a free pass" solves the same proof of work as every new pass
// (new-pass.js), then submits the form; the server makes the pass (within the same limits as POST /v1/keys).

import { solve } from "./new-pass.js";

const form = document.getElementById("create-form");
const button = document.getElementById("create-button");
const status = document.getElementById("create-status");

if (form && button) {
  button.disabled = false;
  form.addEventListener("submit", async (event) => {
    if (document.getElementById("create-solution").value) return; // solved: let it go
    event.preventDefault();
    button.disabled = true;
    status.textContent = "Making your pass (a few seconds)…";
    try {
      const response = await fetch("/v1/keys/challenge", { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(String(response.status));
      const challenge = await response.json();
      document.getElementById("create-challenge").value = challenge.challenge;
      document.getElementById("create-solution").value = await solve(challenge.challenge, challenge.bits);
      form.submit();
    } catch {
      button.disabled = false;
      status.textContent = "That didn't work. Try again, or connect a pass you already have.";
    }
  });
}
