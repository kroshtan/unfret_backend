// The home page: "Checked by us recently" (showcase-list.js), live pack prices from /v1/pricing, and the intro-code
// box, which opens checkout for a new API key with the code filled in. Without JavaScript the page still reads well:
// the prices in the HTML are the defaults, and checkout has its own code field.
import "./showcase-list.js";

const INTRO = /^[A-Za-z0-9]{3,24}$/;
const INVITE = /^ufi_[a-z2-7]{32}$/;

async function prices() {
  const response = await fetch("/v1/pricing", { headers: { Accept: "application/json" } });
  if (!response.ok) return;
  const { packs } = await response.json();
  for (const pack of Array.isArray(packs) ? packs : []) {
    const item = document.querySelector(`#prices [data-checks="${Number(pack.checks)}"]`);
    if (!item) continue;
    item.querySelector(".card-price").textContent = pack.price_usd;
    item.querySelector(".crypto-price").textContent = `${pack.crypto_usd} with crypto`;
  }
}

const form = document.getElementById("code-form");
form?.addEventListener("submit", (event) => {
  event.preventDefault(); // the page's policy allows no form posts; this only navigates
  const value = document.getElementById("code-input").value.trim();
  const status = document.getElementById("code-status");
  if (INTRO.test(value)) {
    window.location.href = `/key?code=${encodeURIComponent(value.toUpperCase())}`;
  } else if (INVITE.test(value.toLowerCase())) {
    window.location.href = `/checkout?for=key&code=${encodeURIComponent(value.toLowerCase())}`;
  } else {
    status.textContent = "That doesn't look like a code: an intro code is a word such as FRIENDS, an invite starts with ufi_.";
  }
});

prices().catch(() => {});
