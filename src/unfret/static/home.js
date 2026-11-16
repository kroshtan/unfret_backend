// The home page: "Checked by us recently" (showcase-list.js), live pack prices from /v1/pricing, and the intro-code
// box, which opens checkout for a new API key with the code filled in. Without JavaScript the page still reads well:
// the prices in the HTML are the defaults, and checkout has its own code field.
import "./showcase-list.js";

const INTRO = /^[A-Za-z0-9]{3,24}$/;
const INVITE = /^ufi_[a-z2-7]{32}$/;

// The card price follows Cloudflare's country hint only visibly: euro prices come with a note naming the country,
// which the visitor can change at checkout.
function countryName(code) {
  if (code === "NL") return "the Netherlands";
  try {
    return new Intl.DisplayNames(["en"], { type: "region" }).of(code) ?? code;
  } catch {
    return code;
  }
}

async function prices() {
  const response = await fetch("/v1/pricing", { headers: { Accept: "application/json" } });
  if (!response.ok) return;
  const body = await response.json();
  const list = Array.isArray(body.packs) ? body.packs : [];
  const hint = body.vat?.country_hint;
  // Euros when the suggested country is in the euro area (as checkout then starts), dollars otherwise; crypto is
  // always in dollars.
  const euros =
    Boolean(hint) && (body.euro_area ?? []).includes(hint) && list.every((pack) => pack.price_eur);
  for (const pack of list) {
    const item = document.querySelector(`#prices [data-checks="${Number(pack.checks)}"]`);
    if (!item) continue;
    item.querySelector(".card-price").textContent = euros ? pack.price_eur : pack.price_usd;
    item.querySelector(".crypto-price").textContent = `${pack.crypto_usd} with crypto`;
  }
  const note = document.getElementById("price-note");
  if (note && euros) {
    const link = document.createElement("a");
    link.href = "/checkout";
    link.textContent = "change at checkout";
    note.replaceChildren(`Card prices in euros for ${countryName(hint)} · `, link);
    note.hidden = false;
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
