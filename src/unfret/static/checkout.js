// /checkout: buy a pack of checks with SOL or USDC (or, for API keys, by card). The wallet signs and sends a transfer
// to Unfret's hot wallet; the server credits the pack once the transfer is final. Phone wallets can pay the same
// request by QR code. Either way the purchase has a quote, and the page polls /v1/credits/quotes/<id> to show the
// payment arriving (no sign-in needed for that). Prices come from /v1/pricing, all derived from the price of one check: each pack has a price
// (by card) and a lower crypto price, plus VAT for the buyer's country (prefilled from where the request comes from).
// Checks go to an API key by default (?for=key; ?for=wallet or a wallet signed in on /connect picks the wallet): a new one (made here after a small proof of work), an existing
// one, or one recovered with its recovery code. The purchase's quote then names the key's public ID; the key itself
// never leaves this page except to check its balance (and to redeem an invite for it, "Have an invite?").
// Invites: every pack comes with invite codes to give away. The purchase comes with a claim token (never on chain,
// never sent to the card provider); the first status poll after payment that carries it (X-Unfret-Claim) returns
// the codes, once. Pending claims are kept in localStorage for a few days, so a closed tab or the card provider's
// round trip doesn't lose them; each is dropped once its purchase is paid.
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

const payPanel = document.getElementById("pay-panel");
const payMessage = document.getElementById("pay-message");
const payOptions = document.getElementById("pay-options");
const paySetup = document.getElementById("pay-setup");
const packs = document.getElementById("packs");
const summary = document.getElementById("summary");
const list = document.getElementById("wallets");
const empty = document.getElementById("no-wallets");
const qr = document.getElementById("qr");
const qrShow = document.getElementById("qr-show");
const payLink = document.getElementById("pay-link");

function onPhone() {
  return window.matchMedia("(pointer: coarse)").matches;
}

if (onPhone()) {
  // The wallet is on this same phone: open it with the payment instead of showing a code to scan.
  document.getElementById("phone-heading").textContent = "With a wallet app on this phone";
  document.getElementById("phone-help").textContent =
    "Phantom, Solflare or any wallet that supports Solana Pay opens with the payment filled in. (On a computer, this shows a QR code to scan.)";
  qrShow.textContent = "Pay in my wallet app";
}
const shop = document.getElementById("shop");
const unavailable = document.getElementById("unavailable");
const currencyField = document.getElementById("currency");
const countryField = document.getElementById("country");
const usdcNote = document.getElementById("usdc-note");
const keyPanel = document.getElementById("key-panel");
const keyInput = document.getElementById("key-input");
const keyStatus = document.getElementById("key-status");
const keyReveal = document.getElementById("key-reveal");
const saved = document.getElementById("saved");
const cardPay = document.getElementById("card-pay");
const card = document.getElementById("card");

let pricing = null;
let pack = null; // the chosen pack from /v1/pricing
let currency = "SOL";
// Signed in on /connect in this tab: phone payments credit that wallet, whoever pays.
const signedIn = recallAny();
// API key is the default (it can pay by card); a wallet when asked for (?for=wallet) or already signed in here.
const forParam = new URLSearchParams(window.location.search).get("for");
let forKey = forParam === "key" || (forParam !== "wallet" && !signedIn);
let fullKey = null; // the whole key, when typed or made here: lets the page show the balance
let watch = null; // the purchase being waited for (see watchQuote)

const POLL_MS = 3000;
const POLL_ROUNDS = 60; // about three minutes of waiting while the page is visible
const CLAIMS_KEY = "unfret-invite-claims";
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

// The key ID to credit, or null. A whole key is reduced to its ID before anything is sent for a purchase.
function keyId() {
  const value = keyInput.value.trim();
  const match = KEY_RE.exec(value);
  fullKey = match ? value : null;
  if (match) return match[1];
  return KEY_ID_RE.test(value) ? value : null;
}

// Paying needs a target (the paying wallet, or a valid key whose new codes are saved first) and a country.
// Paying by card needs a valid key whose new codes are saved first (no country: the provider asks for it).
function cardReady() {
  return forKey && Boolean(pack) && Boolean(keyId()) && (keyReveal.hidden || saved.checked);
}

// The card option is for API keys, when the card provider is configured.
function cardOffered() {
  return forKey && Boolean(pricing?.fiat?.available);
}

function ready() {
  if (!countryField.value) return false;
  if (!forKey) return true;
  return Boolean(keyId()) && (keyReveal.hidden || saved.checked);
}

function refreshTarget() {
  keyPanel.hidden = !forKey;
  card.hidden = !cardOffered();
  const ok = ready();
  list.querySelectorAll("button").forEach((button) => (button.disabled = !ok));
  qrShow.disabled = !ok;
  cardPay.disabled = !cardReady();
  if (forKey && !keyId() && keyInput.value.trim()) setStatus(keyStatus, "That isn't an API key or key ID.", "error");
  else if (forKey && !keyId()) setStatus(keyStatus, "Enter your API key or its ID, or create a new key, to pay.");
  else if (forKey && !keyReveal.hidden && !saved.checked) setStatus(keyStatus, "Save both codes, then tick the box.");
  else setStatus(keyStatus, "");
  describe();
}

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

function reveal(issued) {
  document.getElementById("key-value").textContent = issued.key;
  document.getElementById("recovery-value").textContent = issued.recovery_code;
  keyReveal.hidden = false;
  saved.checked = false;
  keyInput.value = issued.key;
  refreshTarget();
}

async function createKey(button) {
  button.disabled = true;
  try {
    setStatus(keyStatus, "Making your key (a few seconds of work in this browser keeps out bots)…");
    const challenge = await api("/v1/keys/challenge");
    const solution = await solve(challenge.challenge, challenge.bits);
    reveal(await api("/v1/keys", { method: "POST", body: JSON.stringify({ challenge: challenge.challenge, solution }) }));
  } catch (error) {
    setStatus(keyStatus, error.message || "Couldn't make a key; try again.", "error");
  } finally {
    button.disabled = false;
  }
}

async function recoverKey(button) {
  button.disabled = true;
  try {
    const code = document.getElementById("recovery-input").value.trim();
    const issued = await api("/v1/keys/recover", { method: "POST", body: JSON.stringify({ recovery_code: code }) });
    reveal(issued);
    document.getElementById("recover-panel").hidden = true;
  } catch (error) {
    setStatus(keyStatus, error.message || "That recovery code didn't work.", "error");
  } finally {
    button.disabled = false;
  }
}

document.getElementById("new-key").addEventListener("click", (event) => createKey(event.currentTarget));
document.getElementById("recover").addEventListener("click", (event) => recoverKey(event.currentTarget));
document.getElementById("recover-open").addEventListener("click", () => {
  document.getElementById("recover-panel").hidden = false;
});
document.querySelectorAll("[data-copy]").forEach((button) =>
  button.addEventListener("click", () =>
    navigator.clipboard.writeText(document.getElementById(button.dataset.copy).textContent),
  ),
);
keyInput.addEventListener("input", () => {
  keyReveal.hidden = true;
  refreshTarget();
});
saved.addEventListener("change", refreshTarget);
document.getElementById("beneficiary").addEventListener("change", (event) => {
  forKey = event.target.value === "key";
  refreshTarget();
});

// ── Pending invite claims (localStorage; try/catch: private modes must still work, just without remembering) ──

function loadClaims() {
  try {
    const list = JSON.parse(localStorage.getItem(CLAIMS_KEY) ?? "[]");
    return Array.isArray(list) ? list.filter((c) => c && c.id && c.claim && Date.now() - c.at < CLAIM_MAX_AGE_MS) : [];
  } catch {
    return [];
  }
}

function saveClaims(list) {
  try {
    if (list.length) localStorage.setItem(CLAIMS_KEY, JSON.stringify(list));
    else localStorage.removeItem(CLAIMS_KEY);
  } catch {
    // not remembered: this page still shows the invites if it stays open
  }
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

// ── Have an invite? (for an API key: it needs the whole key, which proves the key is yours) ────────────────────

// A shared intro-code link (/key?code=FRIENDS → /checkout?for=key&code=FRIENDS): open the box with the code filled in.
{
  const shared = new URLSearchParams(window.location.search).get("code");
  if (shared && INVITE_RE.test(shared.trim().toLowerCase())) {
    document.getElementById("invite-input").value = shared.trim().toUpperCase();
    document.getElementById("invite-key").open = true;
    setStatus(
      document.getElementById("invite-status"),
      `Your code ${shared.trim().toUpperCase()} is filled in: create a new key above, save it, then press "Use code".`,
    );
  }
}

document.getElementById("invite-redeem").addEventListener("click", async (event) => {
  const status = document.getElementById("invite-status");
  const input = document.getElementById("invite-input");
  const code = input.value.trim().toLowerCase();
  keyId(); // sets fullKey from the key field
  if (!INVITE_RE.test(code)) return setStatus(status, NOT_A_CODE, "error");
  if (!fullKey) {
    return setStatus(
      status,
      "Create a new key above, or paste the whole key (ufk_…, not just its ID): an invite needs the key itself.",
      "error",
    );
  }
  const redeemButton = event.currentTarget;
  redeemButton.disabled = true;
  try {
    const added = await redeemInvite(code, fullKey);
    input.value = "";
    setStatus(
      status,
      `✓ ${checksWord(added.checks_added)} added to key ${keyId()}. It now has ${checksWord(added.checks_remaining)}.`,
      "ok",
    );
  } catch (error) {
    setStatus(status, error.message || "That code didn't work.", "error");
  } finally {
    redeemButton.disabled = false;
  }
});

cardPay.addEventListener("click", async () => {
  const id = keyId();
  if (!id || !cardReady()) return refreshTarget();
  const payButton = cardPay;
  payButton.disabled = true;
  try {
    const started = await api("/v1/credits/fiat-checkout", {
      method: "POST",
      body: JSON.stringify({ key_id: id, checks: pack.checks }),
    });
    // Kept across the provider's checkout: back on /checkout?paid=1 it collects the pack's invites.
    rememberClaim("fiat", started.checkout_id, started.claim, pack.checks);
    window.location.assign(started.checkout_url);
  } catch (error) {
    showMessage("error", error.message || "Card payment isn't available right now.");
    payButton.disabled = false;
  }
});

function formatUsd(micros) {
  return `$${(micros / 1e6).toFixed(2)}`;
}

function checksWord(n) {
  return `${n} ${n === 1 ? "check" : "checks"}`;
}

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
  document.getElementById("card-price").textContent = formatUsd(pack.price_usd_micros);
  document.getElementById("card-tax").textContent = "plus sales tax/VAT where applicable, added at checkout";
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
      // The card price is the price; crypto is shown as the discount (never as a card surcharge).
      const name = document.createElement("strong");
      name.textContent = ` ${checksWord(option.checks)}`;
      const price = document.createElement("span");
      price.className = "price";
      price.textContent = option.price_usd;
      const crypto = document.createElement("span");
      crypto.className = "muted crypto";
      crypto.textContent = `or ${option.crypto_usd} with crypto (+ VAT where applicable)`;
      label.append(input, name, price, crypto);
      if (option.invites?.count) label.append(element("span", inviteOffer(option.invites), "muted gift"));
      return label;
    }),
  );
  refreshTarget();
}

// "comes with 1 invite of 3 checks to give away": always called an invite.
function inviteOffer(invites) {
  const what = invites.count === 1 ? "1 invite" : `${invites.count} invites`;
  return `comes with ${what} of ${invites.checks_each} checks to give away`;
}

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
  const blank = new Option("Choose…", "");
  countryField.replaceChildren(blank, ...options.map(({ code, name }) => new Option(name, code)));
  const hint = pricing.vat.country_hint;
  if (hint && COUNTRIES.includes(hint)) countryField.value = hint;
}

countryField.addEventListener("change", refreshTarget);
currencyField.addEventListener("change", (event) => {
  currency = event.target.value === "SOL" && pricing.sol ? "SOL" : "USDC";
  refreshTarget();
});

function purchaseTarget() {
  if (forKey) return { key_id: keyId() };
  return signedIn ? { beneficiary: signedIn.address } : {};
}

// ── The payment's progress, in step 3 ───────────────────────────────────────────────────────────────────────────

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(text, onClick) {
  const node = element("button", text);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

// Show the panel in one of its states: "info", "waiting", "ok", "timeout", "thanks" or "error". While waiting or
// done, the panel takes the place of the payment choices; a hint or an error shows above them, to try again.
function showPanel(state, ...nodes) {
  payPanel.dataset.state = state;
  payMessage.replaceChildren(...nodes);
  payPanel.hidden = false;
  const busy = ["waiting", "ok", "timeout"].includes(state);
  payOptions.hidden = busy;
  paySetup.hidden = busy;
  card.hidden = busy || !cardOffered();
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
  card.hidden = !cardOffered();
}

function stopWatching() {
  if (watch) clearTimeout(watch.timer);
  watch = null;
}

// Back to choosing: stop waiting (the quote stays valid; paying it later still credits the checks).
function reset() {
  stopWatching();
  clearPanel();
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
  const close = invites.length ? "Once you've copied your invites, you can close this page" : "You can close this page";
  if (purchase.keyId) {
    if (purchase.newKey) {
      const key = element("p", "Your API key");
      key.append(document.createElement("br"), element("code", purchase.newKey));
      nodes.push(
        key,
        element("p", "Keep your recovery code somewhere safe: it gets you a new key with the same balance if you lose this one."),
      );
    }
    nodes.push(
      element("p", `Paste the key in the extension's settings (or use it in scripts) and the extension shows your checks. ${close}.`),
    );
  } else {
    nodes.push(element("p", `${close}; the extension shows your checks.`));
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
    newKey: forKey && !keyReveal.hidden ? fullKey : null, // made or recovered on this page just now
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
  list.querySelectorAll("button").forEach((button) => (button.disabled = true));
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

async function start() {
  try {
    pricing = await api("/v1/pricing");
  } catch {
    pricing = null;
  }
  if (!pricing?.hot_wallet) {
    unavailable.hidden = false;
    return;
  }
  shop.hidden = false;
  if (pricing.network !== "solana-mainnet") {
    document.getElementById("network").textContent = "Test network (devnet): no real SOL.";
  }
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
  document.getElementById("beneficiary").querySelector(`input[value="${forKey ? "key" : "wallet"}"]`).checked = true;
  if (new URLSearchParams(window.location.search).get("paid")) {
    showPanel(
      "thanks",
      element("p", "Thanks! Your card payment is being confirmed; the checks reach your key within a minute.", "headline"),
    );
    void watchCard();
  } else {
    void collectEarlier();
  }
  renderCountries();
  renderPacks();
  renderWallets([]);
  discoverWallets(renderWallets);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Back from the card provider: wait for the webhook to credit the key, then show the pack's invites.
async function watchCard() {
  const entry = loadClaims()
    .filter((c) => c.kind === "fiat")
    .sort((a, b) => b.at - a.at)[0];
  if (!entry) return;
  for (let round = 0; round < POLL_ROUNDS; round += 1) {
    const status = await collectClaim(entry);
    if (status === null) return;
    if (status.paid) {
      const checks = status.checks ?? entry.checks;
      const nodes = [element("p", `✓ Card payment received: ${checksWord(checks)} added to your key.`, "headline")];
      if (status.invites?.length) nodes.push(...inviteNodes(status.invites));
      showPanel("ok", ...nodes, button("Buy more", () => leaveInvites(status.invites ?? [])));
      bringIntoView();
      return;
    }
    await sleep(POLL_MS);
  }
}

// Invites from an earlier purchase on this browser whose page was closed before they were shown.
async function collectEarlier() {
  const codes = [];
  for (const entry of loadClaims()) {
    const status = await collectClaim(entry);
    if (status?.paid && status.invites?.length) codes.push(...status.invites);
  }
  if (!codes.length || watch) return;
  showPanel("thanks", element("p", "Your earlier purchase came with invites", "headline"), ...inviteNodes(codes));
}

start();
