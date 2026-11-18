// Card prices as a buyer pays them. The card provider (Polar, the Merchant of Record) adds VAT to a pack's price at
// checkout, so a price shown without it would go up at the last step: exactly what Unfret warns shoppers about. For
// a country in the EU the price is shown with that country's standard VAT rate, which is what the provider charges
// a consumer there (/v1/pricing's vat.card_rates_pct; not eu_rates_pct, which is what Unfret itself charges on a
// crypto purchase), rounded half up to the cent; elsewhere tax depends on the buyer's state or country, and is
// named as added at checkout.

/** The VAT rate (percent, e.g. 21 or 25.5) a buyer from `country` pays by card, or null outside the EU. */
export function vatPct(pricing, country) {
  const pct = pricing?.vat?.card_rates_pct?.[country];
  return pct === undefined || pct === null ? null : Number(pct);
}

/** `cents` plus `pct` percent VAT, rounded half up to the cent. */
export function withVat(cents, pct) {
  const bps = Math.round(Number(pct) * 100);
  return cents + Math.floor((cents * bps + 5000) / 10000);
}

/** Cents as a price in `currency` ("eur" or "usd"): "€6.66", "$7.38". */
export function formatCents(cents, currency) {
  const amount = (cents / 100).toFixed(2);
  return currency === "eur" ? `€${amount}` : `$${amount}`;
}

/** A pack's card price in `currency` before tax, in cents, or null when it has none in that currency. */
export function netCents(pack, currency) {
  if (currency === "eur") return Number.isInteger(pack.price_eur_cents) ? pack.price_eur_cents : null;
  return Number.isInteger(pack.price_usd_micros) ? Math.round(pack.price_usd_micros / 10_000) : null;
}

/**
 * A pack's card price for a buyer from `country` paying in `currency`: { text, cents, vat } with the VAT included
 * for an EU country (vat: the rate), without any tax elsewhere (vat: null). Null when the pack has no such price.
 */
export function cardPrice(pricing, pack, currency, country) {
  const net = netCents(pack, currency);
  if (net === null) return null;
  const pct = vatPct(pricing, country);
  const cents = pct === null ? net : withVat(net, pct);
  return { text: formatCents(cents, currency), cents, vat: pct };
}

/** What the card price includes, in words, for `country` ("" for no country yet). */
export function taxNote(pricing, country) {
  if (!country) return "Choose your country to see the price with VAT.";
  const pct = vatPct(pricing, country);
  if (pct !== null) return `Card prices include ${pct}% VAT for ${countryPhrase(country)}.`;
  return "Sales tax or VAT is added at checkout where it applies.";
}

// Country names that take "the" in a sentence ("VAT for the Netherlands").
const WITH_THE = new Set(["AE", "BS", "CD", "CF", "DO", "GB", "GM", "KM", "KY", "MH", "MV", "NL", "PH", "SB", "TC", "US", "VA", "VG", "VI"]);

/** A country's English name for use in a sentence: "the Netherlands", "Germany"; the code without Intl support. */
export function countryPhrase(code) {
  let name = code;
  try {
    name = new Intl.DisplayNames(["en"], { type: "region" }).of(code) ?? code;
  } catch {
    // older browsers: the code
  }
  if (code === "CZ") return "Czechia";
  return WITH_THE.has(code) ? `the ${name}` : name;
}

/**
 * The pack a `?checks=<n>` link asks for: that pack if it is on sale, else the one closest in size (the smaller on a
 * tie), with `replaced: true` (an old link to a pack that's gone, such as the 50-pack). Null without a size or packs.
 */
export function packFor(packs, checks) {
  const asked = Number(checks) || 0;
  if (asked <= 0 || !packs?.length) return null;
  const exact = packs.find((p) => p.checks === asked);
  if (exact) return { pack: exact, replaced: false };
  const distance = (p) => Math.abs(p.checks - asked);
  const nearest = [...packs].sort((a, b) => distance(a) - distance(b) || a.checks - b.checks)[0];
  return { pack: nearest, replaced: true };
}
