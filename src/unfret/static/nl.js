// The Dutch landing page (/nl): the paste-a-link field (start-check.js) and the free checks and euro pack prices,
// live from /v1/pricing, VAT included (prices.js). Without JavaScript the page still reads well: the numbers in the
// HTML are the defaults.
import { cardPrice } from "./prices.js";
import { wireStartForm } from "./start-check.js";

const form = document.getElementById("start-check");
wireStartForm(form, { empty: form?.dataset.empty ?? "", invalid: form?.dataset.invalid ?? "" });

/** Cents as a Dutch euro price: "€ 6,00". */
export function euros(cents) {
  return `€ ${(cents / 100).toFixed(2).replace(".", ",")}`;
}

async function prices() {
  const response = await fetch("/v1/pricing", { headers: { Accept: "application/json" } });
  if (!response.ok) return;
  const body = await response.json();
  const free = Number(body.free?.checks_per_pass) || 0;
  if (free > 0) for (const node of document.querySelectorAll("[data-free-count]")) node.textContent = String(free);
  const vat = body.vat?.card_rates_pct?.NL;
  if (vat !== undefined && vat !== null) for (const node of document.querySelectorAll("[data-vat-nl]")) node.textContent = String(vat);
  for (const pack of Array.isArray(body.packs) ? body.packs : []) {
    const item = document.querySelector(`#prices [data-checks="${Number(pack.checks)}"]`);
    if (!item) continue;
    const price = cardPrice(body, pack, "eur", "NL");
    if (price) item.querySelector(".card-price").textContent = euros(price.cents);
    const gifts = Number(pack.invites?.count) || 0;
    const gift = item.querySelector(".gift");
    if (gift) {
      gift.textContent = gifts === 1 ? "met 1 cadeaucode voor een vriend" : `met ${gifts} cadeaucodes voor vrienden`;
      gift.hidden = gifts === 0;
    }
  }
}

prices().catch(() => {});
