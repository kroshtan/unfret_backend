// The home page: live pack prices from /v1/pricing, and the code box, which opens checkout for a new pass (an API
// key) with the code filled in. Without JavaScript the page still reads well: the prices in the HTML are the
// defaults, and checkout has its own code field.
import { cardPrice, taxNote } from "./prices.js";

const INTRO = /^[A-Za-z0-9]{3,24}$/;
const INVITE = /^ufi_[a-z2-7]{32}$/;

// The card prices follow Cloudflare's country hint, visibly: euros in the euro area, and with the VAT the card
// provider adds for an EU country (prices.js), with a note naming the country, which the visitor can change at
// checkout. Elsewhere the dollar prices, with tax added at checkout where it applies.
async function prices() {
  const response = await fetch("/v1/pricing", { headers: { Accept: "application/json" } });
  if (!response.ok) return;
  const body = await response.json();
  const list = Array.isArray(body.packs) ? body.packs : [];
  const hint = body.vat?.country_hint ?? "";
  const euros = Boolean(hint) && (body.euro_area ?? []).includes(hint) && list.every((pack) => pack.price_eur);
  for (const pack of list) {
    const item = document.querySelector(`#prices [data-checks="${Number(pack.checks)}"]`);
    if (!item) continue;
    const price = cardPrice(body, pack, euros ? "eur" : "usd", hint);
    if (price) item.querySelector(".card-price").textContent = price.text;
  }
  const note = document.getElementById("price-note");
  if (note && hint) {
    const link = document.createElement("a");
    link.href = "/checkout";
    link.textContent = "change at checkout";
    note.replaceChildren(`${taxNote(body, hint)} `, link);
    note.hidden = false;
  }
}

// One main button for the visitor's browser rather than a row of stores: Chrome's store for Chrome and its
// relatives (Edge, Brave, Opera and Vivaldi install from it too), Firefox Add-ons for Firefox, and the link checker,
// which installs as an app, on a phone. The other ways stay one line below. Without JavaScript: Chrome.
function browserChoice() {
  const agent = navigator.userAgent;
  const phone = /Android|iPhone|iPad|iPod/i.test(agent) || navigator.userAgentData?.mobile === true;
  if (phone) return "phone";
  if (/Firefox\//.test(agent)) return "firefox";
  if (/Edg\//.test(agent)) return "edge";
  if (/OPR\//.test(agent)) return "opera";
  return "chrome";
}

function chooseButtons() {
  const choice = browserChoice();
  const chrome = document.querySelector('[data-get="main"]')?.href ?? "";
  const firefox = document.querySelector('[data-get="also"] a')?.href ?? "";
  for (const main of document.querySelectorAll('[data-get="main"]')) {
    if (choice === "phone") {
      main.href = "/check";
      main.textContent = "Use Unfret on your phone";
      main.removeAttribute("rel");
    } else if (choice === "firefox") {
      main.href = firefox;
      main.textContent = "Add to Firefox";
    } else if (choice === "edge" || choice === "opera") {
      main.textContent = choice === "edge" ? "Add to Edge" : "Add to Opera";
    }
  }
  for (const link of document.querySelectorAll('[data-get="link"]')) link.hidden = choice === "phone";
  for (const also of document.querySelectorAll('[data-get="also"]')) {
    const store = document.createElement("a");
    store.rel = "noopener";
    if (choice === "firefox") {
      store.href = chrome;
      store.textContent = "Chrome, Edge and Brave";
    } else {
      store.href = firefox;
      store.textContent = "Firefox";
    }
    const phone = document.createElement("a");
    phone.href = "/extension#phone";
    phone.textContent = "your phone";
    if (choice === "phone") {
      const computer = document.createElement("a");
      computer.href = "/extension";
      computer.textContent = "Chrome, Firefox and other browsers";
      also.replaceChildren("On a computer: add Unfret to ", computer, ".");
    } else {
      also.replaceChildren("Also for ", store, " and ", phone, ".");
    }
  }
}

chooseButtons();

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
    status.textContent = "That doesn't look like a code. Check it and try again.";
  }
});

prices().catch(() => {});
