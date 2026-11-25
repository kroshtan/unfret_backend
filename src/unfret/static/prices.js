// Top-up prices as a buyer pays them. A top-up is an amount before VAT (/v1/pricing's topup: the price of a check, the
// least amount, the bonus tiers); the card provider (Polar, the Merchant of Record) adds VAT at checkout, so a price
// shown without it would go up at the last step, exactly what Unfret warns shoppers about. For a country in the EU the
// price is shown with that country's standard VAT rate, which is what the provider charges a consumer there
// (/v1/pricing's vat.card_rates_pct; not eu_rates_pct, which is what Unfret itself charges on a crypto purchase),
// rounded half up to the cent; elsewhere tax depends on the buyer's state or country, and is named as added at checkout.

/** The VAT rate (percent, e.g. 21 or 25.5) a buyer from `country` pays by card, or null outside the EU. */
export function vatPct(pricing, country) {
  const pct = pricing?.vat?.card_rates_pct?.[country];
  return pct === undefined || pct === null ? null : Number(pct);
}

/** Whether the card prices in `currency` ("eur" or "usd") already include VAT/sales tax. */
export function taxIncluded(pricing, currency) {
  return pricing?.card_tax_inclusive?.[currency] === true;
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

/**
 * What the card price includes, in words, for `country` (none chosen yet: asks for one) paying in `currency`
 * (euro prices that include VAT need no country to be final).
 */
export function taxNote(pricing, country, currency = "usd") {
  if (taxIncluded(pricing, currency)) return currency === "eur" ? "Card prices include VAT." : "Card prices include tax.";
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
 * What a top-up of `amount` (before VAT, in `currency`) buys, as /v1/pricing's topup says (the server's own sums
 * decide): { amount, cents, checks, bonus, pct } — floor(amount / price per check) checks, plus the bonus of the highest
 * tier the amount reaches. Null without a price for the currency.
 */
export function topupFor(pricing, amount, currency) {
  const price = Number(pricing?.topup?.price_per_check?.[currency]);
  const value = Math.floor(Number(amount) * 100 + 1e-6) / 100;
  if (!price || !(value > 0)) return null;
  const base = Math.floor(Math.round(value * 1e6) / Math.round(price * 1e6) + 1e-9);
  const tiers = pricing.topup.bonus ?? [];
  const pct = Math.max(0, ...tiers.filter((t) => value >= Number(t.from_amount)).map((t) => Number(t.pct)));
  const bonus = Math.floor((base * pct) / 100);
  return { amount: value, cents: Math.round(value * 100), checks: base + bonus, bonus, pct };
}

/** The top-up in one line, with the VAT for `country` once known: "€100.00 → 1,150 checks · €121.00 incl. 21% VAT". */
export function topupLine(pricing, top, currency, country, { crypto = false } = {}) {
  const checks = `${top.checks.toLocaleString("en-US")} checks${top.bonus ? ` (incl. ${top.bonus.toLocaleString("en-US")} bonus)` : ""}`;
  const head = `${formatCents(top.cents, currency)} → ${checks}`;
  if (!country) return `${head} · plus VAT`;
  const rates = crypto ? pricing?.vat?.eu_rates_pct : pricing?.vat?.card_rates_pct;
  const pct = rates?.[country];
  if (pct === undefined || pct === null) {
    return crypto ? `${head} · no VAT outside the EU` : `${head} · sales tax or VAT added at checkout where it applies`;
  }
  return `${head} · ${formatCents(withVat(top.cents, pct), currency)} incl. ${Number(pct)}% VAT (${country})`;
}
