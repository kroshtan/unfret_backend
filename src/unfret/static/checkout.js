// /checkout: get checks, in four tabs so each visitor sees one short path.
//
//   Buy checks      pick a pack, pay by card. The key is made in this browser just before the card checkout and shown
//                   after paying, so nobody has to understand keys before they've paid.
//   I have a code   one field and one button: a new key is made and the invite or intro code is redeemed onto it.
//   Top up my key   paste the key you have, pick a pack, pay by card (and recover a lost key with its recovery code).
//   Pay with crypto SOL or USDC to a wallet or a key, from a wallet in this browser or by QR code on a phone.
//
// A key made here waits in localStorage ("unfret-new-key") until its owner says it's saved, so a card checkout's round
// trip or a closed tab can't lose it; it is also signed in on /check (if nothing else is) so a link can be checked at
// once. Invites: every pack comes with invite codes to give away. The purchase comes with a claim token (never on
// chain, never sent to the card provider); the first status poll after payment that carries it (X-Unfret-Claim)
// returns the codes, once. Pending claims are kept in localStorage for a few days and dropped once paid. Crypto
// payments have a quote, polled at /v1/credits/quotes/<id> until the transfer is final.
import {
  INVITE_RE,
  NOT_A_CODE,
  api,
  chainOf,
  discoverWallets,
  formatSol,
  fromBase64,
  inviteNodes,
  recall,
  recallAny,
  redeemInvite,
  setStatus,
  walletButton,
} from "./wallet.js";

const $ = (id) => document.getElementById(id);
const main = document.querySelector("main.checkout");
const payPanel = $("pay-panel");
const payMessage = $("pay-message");
const payOptions = $("pay-options");
const paySetup = $("pay-setup");
const packs = $("packs");
const summary = $("summary");
const list = $("wallets");
const empty = $("no-wallets");
const qr = $("qr");
const qrShow = $("qr-show");
const payLink = $("pay-link");
const shop = $("shop");
const currencyField = $("currency");
const countryField = $("country");
const usdcNote = $("usdc-note");
const keyPanel = $("key-panel");
const keyInput = $("key-input");
const keyStatus = $("key-status");
const keyReveal = $("key-reveal");
const savedRow = $("saved-row");
const saved = $("saved");
const newPay = $("new-pay");
const topupPay = $("topup-pay");
const newLocal = $("new-local");
const topupLocal = $("topup-local");
const topupKey = $("topup-key");

const MODES = ["new", "code", "topup", "crypto"];
let mode = "new";
let pricing = null;
let pack = null; // the chosen pack from /v1/pricing
let currency = "SOL";
// The country card payments are for: Cloudflare's hint as a suggestion, shown and changeable ("Paying from: … (change)").
// It decides the card currency and the local method; it lives in this page only and is never stored.
let payCountry = "";
let countryNames = new Map(); // ISO code → English name
// Signed in on /connect in this tab: phone payments credit that wallet, whoever pays.
const signedIn = recallAny();
let forKey = false; // the crypto tab: to a key (true) or to a wallet
let fullKey = null; // the whole key, when typed or made in the crypto tab
let watch = null; // the purchase being waited for (see watchQuote)

const POLL_MS = 3000;
const POLL_ROUNDS = 60; // about three minutes of waiting while the page is visible
const CLAIMS_KEY = "unfret-invite-claims";
const NEW_KEY = "unfret-new-key";
const CHECK_ACCOUNT = "unfret-account"; // /check's sign-in
const CLAIM_MAX_AGE_MS = 3 * 24 * 3600 * 1000; // quotes are gone after about two days anyway

const KEY_RE = /^ufk_([a-z2-7]{10})_[a-z2-7]{52}$/;
const KEY_ID_RE = /^[a-z2-7]{10}$/;
// ISO 3166 alpha-2 codes, named in English by Intl.DisplayNames.
const COUNTRIES = (
  "AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA " +
  "CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA " +
  "GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP " +
  "KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS " +
  "MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS " +
  "RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW " +
  "TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW"
).split(" ");

function onPhone() {
  return window.matchMedia("(pointer: coarse)").matches;
}

if (onPhone()) {
  // The wallet is on this same phone: open it with the payment instead of showing a code to scan.
  $("phone-heading").textContent = "With a wallet app on this phone";
  $("phone-help").textContent =
    "Phantom, Solflare or any wallet that supports Solana Pay opens with the payment filled in. (On a computer, this shows a QR code to scan.)";
  qrShow.textContent = "Pay in my wallet app";
}

// ── Small helpers ───────────────────────────────────────────────────────────────────────────────────────────────

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(text, onClick, className) {
  const node = element("button", text, className);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

function formatUsd(micros) {
  return `$${(micros / 1e6).toFixed(2)}`;
}

function checksWord(n) {
  return `${n} ${n === 1 ? "check" : "checks"}`;
}

// A key's ID from a whole key or the ID itself, or null.
function keyIdOf(value) {
  const text = value.trim();
  const match = KEY_RE.exec(text);
  if (match) return match[1];
  return KEY_ID_RE.test(text) ? text : null;
}

function storageGet(name) {
  try {
    return JSON.parse(localStorage.getItem(name) ?? "null");
  } catch {
    return null;
  }
}

function storageSet(name, value) {
  try {
    if (value === null) localStorage.removeItem(name);
    else localStorage.setItem(name, JSON.stringify(value));
  } catch {
    // private modes: not remembered, this page still works while it stays open
  }
}

// ── Keys made on this page ──────────────────────────────────────────────────────────────────────────────────────

function zeroBits(digest) {
  const bytes = new Uint8Array(digest);
  let bits = 0;
  for (const byte of bytes) {
    if (byte) return bits + Math.clz32(byte) - 24;
    bits += 8;
  }
  return bits;
}

// The proof of work for a new key: SHA-256(challenge + ":" + n) starting with `bits` zero bits, in batches.
async function solve(challenge, bits) {
  const encoder = new TextEncoder();
  for (let start = 0; ; start += 256) {
    const batch = Array.from({ length: 256 }, (_, i) => String(start + i));
    const digests = await Promise.all(
      batch.map((n) => crypto.subtle.digest("SHA-256", encoder.encode(`${challenge}:${n}`))),
    );
    const found = digests.findIndex((digest) => zeroBits(digest) >= bits);
    if (found >= 0) return batch[found];
  }
}

// A new, empty key: { key, recovery_code, key_id }. A few seconds of work in the browser keeps out bots.
async function makeKey() {
  const challenge = await api("/v1/keys/challenge");
  const solution = await solve(challenge.challenge, challenge.bits);
  return api("/v1/keys", { method: "POST", body: JSON.stringify({ challenge: challenge.challenge, solution }) });
}

// The key made here and not yet confirmed saved: { key, recovery_code, key_id, funded }.
function loadNewKey() {
  const stored = storageGet(NEW_KEY);
  return stored && typeof stored.key === "string" && KEY_RE.test(stored.key) ? stored : null;
}

function saveNewKey(value) {
  storageSet(NEW_KEY, value);
}

// An empty key made here earlier is reused (a cancelled card checkout, a refused code); a funded one never is.
async function unfundedKey() {
  const earlier = loadNewKey();
  if (earlier && !earlier.funded) return earlier;
  const issued = await makeKey();
  const fresh = { key: issued.key, recovery_code: issued.recovery_code, key_id: issued.key_id, funded: false };
  saveNewKey(fresh);
  return fresh;
}

// Let /check use the new key right away, unless it is already signed in with something.
function signInCheckPage(issued) {
  if (storageGet(CHECK_ACCOUNT)) return;
  storageSet(CHECK_ACCOUNT, { kind: "key", token: issued.key, address: `key:${issued.key_id}` });
}

function secretRow(label, help, value) {
  const row = element("div", "", "secret");
  const head = element("p", "", "secret-label");
  head.append(element("strong", label));
  if (help) head.append(element("span", help, "muted small"));
  const code = element("code", value);
  const copy = button("Copy", async () => {
    try {
      await navigator.clipboard.writeText(value);
      copy.textContent = "Copied ✓";
      setTimeout(() => (copy.textContent = "Copy"), 2000);
    } catch {
      copy.textContent = "Select and copy it";
    }
  });
  const line = element("div", "", "secret-value");
  line.append(code, copy);
  row.append(head, line);
  return row;
}

// The key and its recovery code, with what to do with them.
function keyCard(issued) {
  const box = element("div", "", "key-card");
  box.append(
    secretRow("Your Unfret key", "", issued.key),
    secretRow("Recovery code", "gets you a new key with the same checks if you ever lose this one", issued.recovery_code),
    element(
      "p",
      "Save both somewhere safe, such as your password manager or a note. We only keep a fingerprint of each, so we can't show them again.",
      "muted small",
    ),
  );
  return box;
}

function useSteps() {
  const steps = element("ol", "", "howto");
  const first = element("li");
  first.append(
    element("b", "In the Unfret extension: "),
    document.createTextNode("click its icon, choose “API key” and paste your key."),
  );
  const second = element("li");
  const link = element("a", "Check a link");
  link.href = "/check";
  second.append(element("b", "On your phone or this page: "), link, document.createTextNode(" (you're signed in here)."));
  steps.append(first, second);
  return steps;
}

// ── Tabs ────────────────────────────────────────────────────────────────────────────────────────────────────────

const tabs = [...document.querySelectorAll('[role="tab"]')];

function setMode(next, { focus = false } = {}) {
  mode = MODES.includes(next) ? next : "new";
  main.dataset.mode = mode;
  for (const tab of tabs) {
    const on = tab.dataset.mode === mode;
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
    if (on && focus) tab.focus();
    $(tab.getAttribute("aria-controls")).hidden = !on;
  }
  $("pack-section").hidden = mode === "code";
  refreshTarget();
}

tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => setMode(tab.dataset.mode));
  tab.addEventListener("keydown", (event) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    if (!step) return;
    event.preventDefault();
    setMode(tabs[(index + step + tabs.length) % tabs.length].dataset.mode, { focus: true });
  });
});

// While a result is on screen (a new key, a payment), the tabs and choices step aside.
function showChoices(visible) {
  for (const node of [document.querySelector(".tabs"), $("pack-section"), ...MODES.map((m) => $(`panel-${m}`))]) {
    node.classList.toggle("set-aside", !visible);
  }
}

// ── Card payments (Buy checks, Top up my key) ───────────────────────────────────────────────────────────────────

function cardAvailable() {
  return Boolean(pricing?.fiat?.available);
}

// Card prices are in euros for the euro area (/v1/pricing's euro_area), in dollars elsewhere, for the chosen country.
function cardCurrency() {
  const euros = (pricing?.euro_area ?? []).includes(payCountry);
  return euros && pricing.packs.every((p) => p.price_eur) ? "eur" : "usd";
}

function priceIn(currency, option) {
  return currency === "eur" ? option.price_eur : option.price_usd;
}

function cardPrice(option) {
  return priceIn(cardCurrency(), option);
}

// The chosen country's local way to pay (/v1/pricing's local_payment_methods, e.g. iDEAL | Wero in the Netherlands),
// shown as its own button above the card one; null for other countries. The server's table says which country has which.
function localMethod() {
  const method = payCountry ? pricing?.local_payment_methods?.[payCountry] : null;
  if (!method?.id || !method.label) return null;
  return pricing.packs.every((p) => priceIn(method.currency, p)) ? method : null;
}

// method: a local method from localMethod(), or null for a card checkout (the buyer picks their country at Polar).
async function startCardCheckout(keyId, method = null) {
  const choice = method
    ? { currency: method.currency, payment_method: method.id }
    : { currency: cardCurrency() };
  const started = await api("/v1/credits/fiat-checkout", {
    method: "POST",
    body: JSON.stringify({ key_id: keyId, checks: pack.checks, ...choice }),
  });
  // Kept across the provider's checkout: back on /checkout?paid=1 it collects the pack's invites.
  rememberClaim("fiat", started.checkout_id, started.claim, pack.checks);
  window.location.assign(started.checkout_url);
}

async function payNew(method) {
  if (!pack || !cardAvailable()) return;
  const status = $("new-status");
  newPay.disabled = newLocal.disabled = true;
  try {
    setStatus(status, "Getting your checkout ready…");
    const issued = await unfundedKey();
    await startCardCheckout(issued.key_id, method);
  } catch (error) {
    setStatus(status, error.message || "Card payment isn't available right now; please try again.", "error");
    newPay.disabled = newLocal.disabled = false;
  }
}

async function payTopup(method) {
  const id = keyIdOf(topupKey.value);
  if (!pack || !id || !cardAvailable()) return refreshTarget();
  topupPay.disabled = topupLocal.disabled = true;
  try {
    await startCardCheckout(id, method);
  } catch (error) {
    setStatus($("topup-status"), error.message || "Card payment isn't available right now.", "error");
    topupPay.disabled = topupLocal.disabled = false;
  }
}

newPay.addEventListener("click", () => payNew(null));
newLocal.addEventListener("click", () => payNew(localMethod()));
topupPay.addEventListener("click", () => payTopup(null));
topupLocal.addEventListener("click", () => payTopup(localMethod()));
topupKey.addEventListener("input", refreshTarget);

// Lost key: the recovery code gets a new key with the same balance (the old key stops working).
$("recover").addEventListener("click", async (event) => {
  const recoverButton = event.currentTarget;
  const status = $("recover-status");
  recoverButton.disabled = true;
  try {
    const code = $("recovery-input").value.trim();
    const issued = await api("/v1/keys/recover", { method: "POST", body: JSON.stringify({ recovery_code: code }) });
    topupKey.value = issued.key;
    $("recovery-input").value = "";
    $("recover-box").open = false;
    showResult(
      "Here's your new key",
      { ...issued, funded: true },
      [element("p", "It has the same checks as your old key, which no longer works. Your recovery code is new too.")],
      { saveFirst: true },
    );
  } catch (error) {
    setStatus(status, error.message || "That recovery code didn't work.", "error");
  } finally {
    recoverButton.disabled = false;
  }
});

// ── I have a code ───────────────────────────────────────────────────────────────────────────────────────────────

function normaliseCode(value) {
  const typed = value.trim();
  if (!INVITE_RE.test(typed.toLowerCase())) return null;
  return typed.toLowerCase().startsWith("ufi_") ? typed.toLowerCase() : typed.toUpperCase();
}

$("invite-redeem").addEventListener("click", async (event) => {
  const redeemButton = event.currentTarget;
  const status = $("invite-status");
  const code = normaliseCode($("invite-input").value);
  if (!code) return setStatus(status, NOT_A_CODE, "error");
  redeemButton.disabled = true;
  try {
    setStatus(status, "Setting up your key…");
    const issued = await unfundedKey();
    const added = await redeemInvite(code, issued.key);
    const funded = { ...issued, funded: true };
    saveNewKey(funded);
    signInCheckPage(funded);
    $("invite-input").value = "";
    setStatus(status, "");
    showResult(`✓ ${checksWord(added.checks_added)} added. Welcome to Unfret!`, funded, []);
  } catch (error) {
    setStatus(status, error.message || "That code didn't work.", "error");
  } finally {
    redeemButton.disabled = false;
  }
});

// ── Results: a new key (after a code, a card payment or a recovery) ─────────────────────────────────────────────

// The result view, in place of the choices: a headline, the key card and how to use it, then "I've saved my key".
function showResult(headline, issued, extra = [], { saveFirst = false, invites = [] } = {}) {
  stopWatching();
  const nodes = [element("p", headline, "headline")];
  if (issued) nodes.push(keyCard(issued), element("h3", "How to use your key"), useSteps());
  nodes.push(...extra);
  if (invites.length) nodes.push(...inviteNodes(invites));
  const done = button(
    issued ? "I've saved my key" : "Done",
    () => {
      if (invites.length && !window.confirm("Copied your invites? They can't be shown again.")) return;
      if (issued && loadNewKey()?.key === issued.key) saveNewKey(null);
      clearPanel();
      showChoices(true);
      setMode(saveFirst ? "topup" : mode);
    },
    "primary",
  );
  nodes.push(element("div", "", "result-actions"));
  nodes.at(-1).append(done);
  showPanel("ok", ...nodes);
  showChoices(false);
  bringIntoView();
}

// ── Crypto: to a key or a wallet ────────────────────────────────────────────────────────────────────────────────

// The key ID to credit from the crypto tab, or null. A whole key is reduced to its ID before anything is sent.
function keyId() {
  const value = keyInput.value.trim();
  fullKey = KEY_RE.test(value) ? value : null;
  return keyIdOf(value);
}

// Paying with crypto needs a country, and for a key a valid one whose new codes are saved first.
function ready() {
  if (!countryField.value) return false;
  if (!forKey) return true;
  return Boolean(keyId()) && (keyReveal.hidden || saved.checked);
}

function refreshTarget() {
  // Card buttons: the price on the button; a key to top up. With a local method, its button comes first and the
  // card one steps back to a plain button.
  const cardOn = cardAvailable();
  const local = cardOn ? localMethod() : null;
  const label = `Pay${pack ? ` ${cardPrice(pack)}` : ""} by card`;
  const localLabel = local ? `Pay${pack ? ` ${priceIn(local.currency, pack)}` : ""} with ${local.label}` : "";
  for (const prefix of CARD_COUNTRY) $(`${prefix}-country-box`).parentElement.hidden = !cardOn;
  for (const price of packs.querySelectorAll(".price.card-only")) {
    const option = pricing?.packs.find((p) => String(p.checks) === price.dataset.checks);
    if (option) price.textContent = cardPrice(option); // follows the chosen country's currency
  }
  const topupId = keyIdOf(topupKey.value);
  for (const [cardButton, localButton, ok] of [
    [newPay, newLocal, Boolean(pack)],
    [topupPay, topupLocal, Boolean(pack && topupId)],
  ]) {
    cardButton.textContent = cardOn ? label : "Card payments are unavailable right now";
    cardButton.classList.toggle("primary", !local);
    cardButton.disabled = !cardOn || !ok;
    localButton.hidden = !local;
    localButton.textContent = localLabel;
    localButton.disabled = !local || !ok;
  }
  const topupStatus = $("topup-status");
  if (topupKey.value.trim() && !topupId) setStatus(topupStatus, "That doesn't look like an Unfret key.", "error");
  else setStatus(topupStatus, "");

  // Crypto.
  keyPanel.hidden = !forKey;
  const ok = ready();
  list.querySelectorAll("button").forEach((b) => (b.disabled = !ok));
  qrShow.disabled = !ok;
  if (forKey && !keyId() && keyInput.value.trim()) setStatus(keyStatus, "That isn't an Unfret key or key ID.", "error");
  else if (forKey && !keyId()) setStatus(keyStatus, "Paste your key or its ID, or make a new key, to pay.");
  else if (forKey && !keyReveal.hidden && !saved.checked) setStatus(keyStatus, "Save your key, then tick the box.");
  else setStatus(keyStatus, "");
  describe();
}

$("new-key").addEventListener("click", async (event) => {
  const makeButton = event.currentTarget;
  makeButton.disabled = true;
  try {
    setStatus(keyStatus, "Making your key (a few seconds of work in this browser keeps out bots)…");
    const issued = await makeKey();
    keyReveal.replaceChildren(keyCard(issued));
    keyReveal.hidden = false;
    savedRow.hidden = false;
    saved.checked = false;
    keyInput.value = issued.key;
    refreshTarget();
  } catch (error) {
    setStatus(keyStatus, error.message || "Couldn't make a key; try again.", "error");
  } finally {
    makeButton.disabled = false;
  }
});
keyInput.addEventListener("input", () => {
  keyReveal.hidden = true;
  savedRow.hidden = true;
  refreshTarget();
});
saved.addEventListener("change", refreshTarget);
$("beneficiary").addEventListener("change", (event) => {
  forKey = event.target.value === "key";
  refreshTarget();
});

// The VAT this page expects for the chosen country (the server's quote is what counts): EU countries per
// /v1/pricing's vat.eu_rates_pct, others none.
function vatFor(country) {
  const pct = Number(pricing.vat.eu_rates_pct[country] ?? 0);
  const net = pack.crypto_usd_micros;
  const vat = Math.floor((net * pct * 100 + 5000) / 10000);
  return { pct, net, vat, total: net + vat };
}

// The amount to pay in the chosen currency's base units for a dollar value: USDC exactly, SOL at the live quote
// (rounded up, as the server prices it).
function payAmount(value) {
  if (currency === "USDC") return value;
  return Math.ceil((value * 1000) / Number(pricing.sol.usd_per_sol));
}

function formatPay(amount) {
  return currency === "SOL" ? formatSol(amount) : `${(amount / 1e6).toFixed(2)} USDC`;
}

function describe() {
  if (!pricing || !pack || watch) return; // while a purchase is under way, the summary describes that one
  if (!countryField.value) {
    summary.textContent = `${checksWord(pack.checks)}: choose your country to see the price with VAT.`;
    return;
  }
  const { pct, net, vat, total } = vatFor(countryField.value);
  const sol = currency === "SOL" ? ` ≈ ${formatPay(payAmount(total))}` : "";
  const price = pct
    ? `${formatUsd(net)} + ${pct}% VAT (${formatUsd(vat)}) = ${formatUsd(total)}`
    : `${formatUsd(total)} (no VAT outside the EU)`;
  summary.textContent = `${checksWord(pack.checks)} with ${currency}: ${price}${sol}.`;
}

// "comes with 1 invite of 3 checks to give away": always called an invite.
function inviteOffer(invites) {
  const what = invites.count === 1 ? "1 invite" : `${invites.count} invites`;
  return `comes with ${what} of ${invites.checks_each} checks to give away`;
}

function renderPacks() {
  // ?checks=<n> preselects a pack.
  const preset = Number(new URLSearchParams(window.location.search).get("checks")) || 0;
  pack = pricing.packs.find((p) => p.checks === preset) ?? pack ?? pricing.packs[0];
  packs.replaceChildren(
    ...pricing.packs.map((option) => {
      const label = document.createElement("label");
      label.className = "pack";
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "pack";
      input.value = String(option.checks);
      input.checked = option.checks === pack.checks;
      input.addEventListener("change", () => {
        pack = option;
        refreshTarget();
      });
      const name = element("strong", ` ${checksWord(option.checks)}`);
      // The card price is the price; the crypto tab shows the crypto price (a discount, never a card surcharge).
      const price = element("span", cardPrice(option), "price card-only");
      price.dataset.checks = String(option.checks);
      const cryptoPrice = element("span", option.crypto_usd, "price crypto-only");
      const crypto = element("span", "+ VAT where it applies", "muted crypto crypto-only");
      label.append(input, name, price, cryptoPrice, crypto);
      if (option.invites?.count) label.append(element("span", inviteOffer(option.invites), "muted gift"));
      return label;
    }),
  );
  refreshTarget();
}

const CARD_COUNTRY = ["new", "topup"]; // the "Paying from" line in Buy checks and Top up my key

function renderCountries() {
  let names = null;
  try {
    names = new Intl.DisplayNames(["en"], { type: "region" });
  } catch {
    // older browsers: codes only
  }
  const options = COUNTRIES.map((code) => ({ code, name: names?.of(code) ?? code })).sort((a, b) =>
    a.name.localeCompare(b.name, "en"),
  );
  countryNames = new Map(options.map(({ code, name }) => [code, name]));
  for (const select of [countryField, ...CARD_COUNTRY.map((p) => $(`${p}-country`))]) {
    select.replaceChildren(new Option("Choose…", ""), ...options.map(({ code, name }) => new Option(name, code)));
  }
  const hint = pricing.vat.country_hint;
  setPayCountry(hint && COUNTRIES.includes(hint) ? hint : "");
}

// One chosen country for the whole page: the card lines and the crypto tab's VAT country follow each other.
function setPayCountry(code) {
  payCountry = COUNTRIES.includes(code) ? code : "";
  countryField.value = payCountry;
  for (const prefix of CARD_COUNTRY) {
    $(`${prefix}-country`).value = payCountry;
    $(`${prefix}-country-from`).hidden = !payCountry;
    $(`${prefix}-country-name`).textContent = payCountry ? countryNames.get(payCountry) : "Choose your country";
    $(`${prefix}-country-change`).textContent = payCountry ? "change" : "choose";
  }
}

for (const prefix of CARD_COUNTRY) {
  const change = $(`${prefix}-country-change`);
  const box = $(`${prefix}-country-box`);
  change.addEventListener("click", () => {
    box.hidden = !box.hidden;
    change.setAttribute("aria-expanded", String(!box.hidden));
    if (!box.hidden) $(`${prefix}-country`).focus();
  });
  $(`${prefix}-country`).addEventListener("change", (event) => {
    setPayCountry(event.target.value);
    refreshTarget();
  });
}

countryField.addEventListener("change", () => {
  setPayCountry(countryField.value);
  refreshTarget();
});
currencyField.addEventListener("change", (event) => {
  currency = event.target.value === "SOL" && pricing.sol ? "SOL" : "USDC";
  refreshTarget();
});

function purchaseTarget() {
  if (forKey) return { key_id: keyId() };
  return signedIn ? { beneficiary: signedIn.address } : {};
}

// ── Pending invite claims (localStorage; try/catch: private modes must still work, just without remembering) ──

function loadClaims() {
  const list = storageGet(CLAIMS_KEY);
  return Array.isArray(list) ? list.filter((c) => c && c.id && c.claim && Date.now() - c.at < CLAIM_MAX_AGE_MS) : [];
}

function saveClaims(list) {
  storageSet(CLAIMS_KEY, list.length ? list : null);
}

function rememberClaim(kind, id, claim, checks) {
  if (!id || !claim) return;
  saveClaims([...loadClaims().filter((c) => c.id !== id), { kind, id, claim, checks, at: Date.now() }]);
}

function forgetClaim(id) {
  saveClaims(loadClaims().filter((c) => c.id !== id));
}

// A purchase's status with its claim: { paid, checks, invites } (the invites only the first time), or null if the
// purchase is unknown (gone, or not this claim). Once paid, the claim has done its job and is forgotten.
async function collectClaim(entry) {
  const path =
    entry.kind === "fiat"
      ? `/v1/credits/fiat-checkouts/${encodeURIComponent(entry.id)}`
      : `/v1/credits/quotes/${encodeURIComponent(entry.id)}`;
  try {
    const status = await api(path, { cache: "no-store", headers: { "X-Unfret-Claim": entry.claim } });
    if (status.paid) forgetClaim(entry.id);
    return status;
  } catch (error) {
    if (error.status === 404) forgetClaim(entry.id);
    return null;
  }
}

// ── The payment's progress ──────────────────────────────────────────────────────────────────────────────────────

// Show the panel in one of its states: "info", "waiting", "ok", "timeout", "thanks" or "error". While waiting or
// done, the panel takes the place of the crypto payment choices; a hint or an error shows above them, to try again.
function showPanel(state, ...nodes) {
  payPanel.dataset.state = state;
  payMessage.replaceChildren(...nodes);
  payPanel.hidden = false;
  const busy = ["waiting", "ok", "timeout"].includes(state);
  payOptions.hidden = busy;
  paySetup.hidden = busy;
}

function showMessage(state, text) {
  qr.hidden = true;
  showPanel(state, element("p", text, "headline"));
}

function bringIntoView() {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  payPanel.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
  payPanel.focus({ preventScroll: true });
}

function clearPanel() {
  payPanel.hidden = true;
  payMessage.replaceChildren();
  qr.hidden = true;
  payLink.hidden = true;
  payOptions.hidden = false;
  paySetup.hidden = false;
}

function stopWatching() {
  if (watch) clearTimeout(watch.timer);
  watch = null;
}

// Back to choosing: stop waiting (the quote stays valid; paying it later still credits the checks).
function reset() {
  stopWatching();
  clearPanel();
  showChoices(true);
  refreshTarget();
}

// Leaving a confirmation that shows invites removes them from the page for good: ask first.
function leaveInvites(invites) {
  if (invites.length && !window.confirm("Copied your invites? They can't be shown again.")) return;
  reset();
}

// Where the checks went, in words, for the success message.
function destination(purchase) {
  if (purchase.keyId) return ` to key ${purchase.keyId}`;
  if (purchase.qr && !purchase.signedIn) return " to the wallet you paid with";
  return " to your wallet";
}

function showWaiting(purchase) {
  const headline = element("p", "", "headline");
  headline.append(element("span", "", "spinner"), element("span", "Waiting for your payment…"));
  const hint = purchase.qr
    ? "Scan the code and approve the payment in your phone's wallet. It shows here once it is final, usually within 30 seconds."
    : "Your payment is on its way. It shows here once it is final, usually within 30 seconds.";
  showPanel("waiting", headline, element("p", hint, "muted"), button("Pay another way", reset));
}

function showTimeout(purchase) {
  const text = purchase.qr
    ? "No payment has arrived yet. If you've paid, it may still be settling: the checks will appear by themselves, so there's no need to pay again."
    : "Your payment may still be settling. The checks will appear by themselves once it is final; there's no need to pay again.";
  showPanel(
    "timeout",
    element("p", "Still waiting for the payment", "headline"),
    element("p", text),
    button("Check again", () => watchQuote(purchase)),
    document.createTextNode(" "),
    button("Pay another way", reset),
  );
}

async function showPaid(purchase, invites = []) {
  qr.hidden = true;
  const headline = `✓ Payment received: ${checksWord(purchase.checks)} added${destination(purchase)}.`;
  const nodes = [element("p", headline, "headline")];
  if (invites.length) nodes.push(...inviteNodes(invites));
  if (purchase.keyId) {
    nodes.push(element("p", "In the extension, choose “API key” and paste your key, and it shows your checks."));
  } else {
    nodes.push(element("p", "The extension shows your checks once you're signed in with this wallet."));
  }
  const balance = element("p", "", "muted");
  nodes.push(balance, button("Buy more", () => leaveInvites(invites)));
  showPanel("ok", ...nodes);
  bringIntoView();
  if (!purchase.token) return;
  try {
    const credits = await api("/v1/credits", { headers: { Authorization: `Bearer ${purchase.token}` } });
    balance.textContent = `Your balance is now ${checksWord(credits.checks_remaining)}.`;
  } catch {
    // the confirmation stands on its own; the balance is a nicety
  }
}

function schedule(purchase, delay) {
  clearTimeout(purchase.timer);
  if (watch !== purchase || document.hidden) return; // resumes when the page is visible again
  purchase.timer = setTimeout(() => poll(purchase), delay);
}

async function poll(purchase) {
  if (watch !== purchase) return;
  purchase.rounds += 1;
  try {
    const headers = purchase.claim ? { "X-Unfret-Claim": purchase.claim } : {};
    const quote = await api(`/v1/credits/quotes/${encodeURIComponent(purchase.quoteId)}`, {
      cache: "no-store",
      headers,
    });
    if (watch !== purchase) return;
    if (quote.paid) {
      forgetClaim(purchase.quoteId);
      stopWatching();
      showPaid(purchase, quote.invites ?? []);
      return;
    }
  } catch {
    // a network blip: keep trying
  }
  if (watch !== purchase) return;
  if (purchase.rounds >= POLL_ROUNDS) {
    purchase.timedOut = true;
    showTimeout(purchase);
    return;
  }
  schedule(purchase, POLL_MS);
}

// Wait for the purchase's quote to be paid: every 3 s for about three minutes, paused while the page is hidden.
function watchQuote(purchase) {
  stopWatching();
  watch = purchase;
  purchase.rounds = 0;
  purchase.timedOut = false;
  summary.textContent = purchase.summary;
  showWaiting(purchase);
  bringIntoView();
  schedule(purchase, POLL_MS);
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && watch && !watch.timedOut) schedule(watch, 0);
});

// What the messages need about the purchase, captured when it starts (before any choice can change).
function purchaseInfo(extra) {
  return {
    checks: pack.checks,
    keyId: forKey ? keyId() : null,
    summary: summary.textContent,
    ...extra,
  };
}

qrShow.addEventListener("click", async () => {
  if (!ready() || !pack) return refreshTarget();
  stopWatching();
  clearPanel();
  const params = new URLSearchParams({ checks: String(pack.checks), country: countryField.value, currency });
  for (const [name, value] of Object.entries(purchaseTarget())) params.set(name, value);
  const info = purchaseInfo({
    qr: true,
    signedIn: Boolean(signedIn) && !forKey,
    token: forKey ? fullKey : signedIn?.token,
  });
  qrShow.disabled = true;
  try {
    // Fetched rather than set as the <img> source, to read the quote's id from the response header.
    const response = await fetch(`/v1/credits/solana-pay.svg?${params}`, { cache: "no-store" });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail ?? `Request failed (${response.status})`);
    }
    const quoteId = response.headers.get("X-Unfret-Quote");
    const claim = response.headers.get("X-Unfret-Claim");
    const payUrl = response.headers.get("X-Unfret-Pay-Url");
    rememberClaim("quote", quoteId, claim, info.checks);
    // A data: URL, not a blob: one: the page's CSP allows images from 'self' and data: only.
    qr.src = `data:image/svg+xml;base64,${btoa(await response.text())}`;
    qr.hidden = false;
    // On a phone the code can't be scanned by the same phone: offer the request as a link that opens the wallet app.
    payLink.hidden = !(payUrl && onPhone());
    if (payUrl) payLink.href = payUrl;
    if (quoteId) {
      watchQuote({ ...info, quoteId, claim });
    } else {
      showPanel("info", element("p", "Scan the code with your phone's wallet. Your checks arrive once the payment is final."));
    }
  } catch (error) {
    showMessage("error", error.message || "Couldn't make the QR code; try again.");
  } finally {
    refreshTarget();
  }
});

async function buy(wallet) {
  if (!pack || !ready()) return;
  stopWatching();
  clearPanel();
  const send = wallet.features["solana:signAndSendTransaction"];
  if (!send) {
    showMessage("error", `${wallet.name} can't send transactions from this page; use the QR code instead.`);
    return;
  }
  const info = purchaseInfo({ qr: false, signedIn: true });
  list.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    const { accounts } = await wallet.features["standard:connect"].connect();
    const account = accounts[0];
    if (!account) throw new Error("No account was shared by the wallet.");
    // No sign-in needed: the quote shows when the payment arrives. A session or whole key also shows the balance.
    info.token = forKey ? fullKey : recall(account.address)?.token;
    const target = info.keyId ? { key_id: info.keyId } : {};
    const purchase = await api("/v1/credits/purchase-tx", {
      method: "POST",
      body: JSON.stringify({
        address: account.address,
        currency,
        checks: info.checks,
        country: countryField.value,
        ...target,
      }),
    });
    rememberClaim("quote", purchase.quote_id, purchase.claim, info.checks);
    showMessage("info", `Approve the payment of ${formatPay(purchase.amount)} (${purchase.price.total_usd}) in ${wallet.name}.`);
    bringIntoView();
    await send.signAndSendTransaction({
      account,
      chain: chainOf(pricing.network),
      transaction: fromBase64(purchase.transaction),
    });
    watchQuote({ ...info, quoteId: purchase.quote_id, claim: purchase.claim });
  } catch (error) {
    showMessage("error", error.message || "The purchase didn't go through.");
  } finally {
    refreshTarget();
  }
}

function renderWallets(wallets) {
  list.replaceChildren(...wallets.map((wallet) => walletButton(wallet, buy)));
  empty.hidden = wallets.length > 0;
  refreshTarget();
}

// ── Start ───────────────────────────────────────────────────────────────────────────────────────────────────────

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Back from the card provider: wait for the webhook to credit the key, then show the key (if it was made here) and
// the pack's invites.
async function watchCard() {
  const entry = loadClaims()
    .filter((c) => c.kind === "fiat")
    .sort((a, b) => b.at - a.at)[0];
  const made = loadNewKey();
  if (!entry) {
    if (made?.funded) showResult("Your Unfret key", made);
    return;
  }
  showChoices(false);
  showPanel("waiting", element("p", "Thanks! Confirming your card payment…", "headline"));
  for (let round = 0; round < POLL_ROUNDS; round += 1) {
    const status = await collectClaim(entry);
    if (status === null) break;
    if (status.paid) {
      const checks = status.checks ?? entry.checks;
      const invites = status.invites ?? [];
      if (made) {
        const funded = { ...made, funded: true };
        saveNewKey(funded);
        signInCheckPage(funded);
        showResult(`✓ Payment received: ${checksWord(checks)} are ready.`, funded, [], { invites });
      } else {
        showResult(`✓ Payment received: ${checksWord(checks)} added to your key.`, null, [], { invites });
      }
      return;
    }
    await sleep(POLL_MS);
  }
  showResult(
    "Your payment is still being confirmed",
    made,
    [element("p", "Your checks arrive by themselves within a few minutes; there's no need to pay again.")],
  );
}

// Invites from an earlier purchase on this browser whose page was closed before they were shown, and a key made
// here that its owner hasn't confirmed saving.
async function collectEarlier() {
  const codes = [];
  for (const entry of loadClaims()) {
    const status = await collectClaim(entry);
    if (status?.paid && status.invites?.length) codes.push(...status.invites);
  }
  if (watch) return;
  const made = loadNewKey();
  if (made?.funded) {
    showResult("Your Unfret key", made, [element("p", "You haven't confirmed saving it yet.", "muted")], {
      invites: codes,
    });
  } else if (codes.length) {
    showResult("Your earlier purchase came with invites", null, [], { invites: codes });
  }
}

function initialMode(params) {
  const asked = params.get("mode");
  if (MODES.includes(asked)) return asked;
  if (params.get("code")) return "code";
  if (params.get("for") === "wallet" || (!params.get("for") && signedIn)) return "crypto";
  return cardAvailable() ? "new" : "crypto";
}

async function start() {
  try {
    pricing = await api("/v1/pricing");
  } catch {
    pricing = null;
  }
  if (!pricing?.hot_wallet) {
    $("unavailable").hidden = false;
    return;
  }
  shop.hidden = false;
  if (pricing.network !== "solana-mainnet") {
    $("network").textContent = "Test network (devnet): no real money.";
  }
  const params = new URLSearchParams(window.location.search);
  const shared = params.get("code");
  if (shared && normaliseCode(shared)) $("invite-input").value = normaliseCode(shared);
  const solRadio = currencyField.querySelector('input[value="SOL"]');
  const usdcRadio = currencyField.querySelector('input[value="USDC"]');
  if (pricing.sol) {
    usdcNote.textContent = `SOL amounts are the dollar prices at ${Number(pricing.sol.usd_per_sol).toFixed(2)} USD/SOL.`;
  } else {
    solRadio.disabled = true;
    usdcRadio.checked = true;
    currency = "USDC";
    usdcNote.textContent = "SOL is unavailable right now (no fresh price); pay in USDC.";
  }
  renderCountries();
  renderPacks();
  renderWallets([]);
  setMode(initialMode(params));
  if (params.get("paid")) void watchCard();
  else void collectEarlier();
  discoverWallets(renderWallets);
}

start();
