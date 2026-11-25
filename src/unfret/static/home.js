// The home page: the paste-a-link field (start-check.js), live pack prices from /v1/pricing, and the install button
// for the visitor's browser. Without JavaScript the page still reads well: the prices in the HTML are the defaults.
import { chooseButtons } from "./install-button.js";
import { cardPrice, taxNote } from "./prices.js";
import { wireStartForm } from "./start-check.js";

// The card prices follow Cloudflare's country hint, visibly: euros in the euro area, dollars elsewhere, each with tax
// included while /v1/pricing's card_tax_inclusive says so (the default). Prices before tax get the VAT the card
// provider adds for an EU country (prices.js), or a note that tax is added at checkout where it applies.
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
  // The free tier in the same currency as the packs.
  const freePrice = document.querySelector("#prices .free .card-price");
  if (freePrice) freePrice.textContent = euros ? "€0" : "$0";
  // The free tier, as configured.
  const free = Number(body.free?.checks_per_pass) || 0;
  if (free > 0) for (const node of document.querySelectorAll("[data-free-count]")) node.textContent = String(free);
  const note = document.getElementById("price-note");
  if (note && hint) {
    const link = document.createElement("a");
    link.href = "/checkout";
    link.textContent = "change at checkout";
    note.replaceChildren(`${taxNote(body, hint, euros ? "eur" : "usd")} `, link);
    note.hidden = false;
  }
}

const form = document.getElementById("start-check");
wireStartForm(form, { empty: form?.dataset.empty ?? "", invalid: form?.dataset.invalid ?? "" });
chooseButtons();
prices().catch(() => {});
