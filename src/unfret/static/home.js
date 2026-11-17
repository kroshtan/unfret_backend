// The home page: live pack prices from /v1/pricing, and the install button for the visitor's browser. Without
// JavaScript the page still reads well: the prices in the HTML are the defaults.
import { chooseButtons } from "./install-button.js";
import { cardPrice, taxNote } from "./prices.js";

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



chooseButtons();
prices().catch(() => {});
