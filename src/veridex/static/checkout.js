// /checkout: buy a pack of checks with SOL or USDC (or, for API keys, by card). The wallet signs and sends a transfer
// to Unfret's hot wallet; the server credits the pack once the transfer is final. Phone wallets can pay the same
// request by QR code. Either way the purchase has a quote, and the page polls /v1/credits/quotes/<id> to show the
// payment arriving (no sign-in needed for that). Prices come from /v1/pricing, all derived from the price of one check: each pack has a price
// (by card) and a lower crypto price, plus VAT for the buyer's country (prefilled from where the request comes from).
// Checks can go to an API key instead (?for=key): a new one (made here after a small proof of work), an existing
// one, or one recovered with its recovery code. The purchase's quote then names the key's public ID; the key itself
// never leaves this page except to check its balance.
import {
  api,
  chainOf,
  discoverWallets,
  formatSol,
  fromBase64,
  recall,
  recallAny,
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
const card = document.getElementById("card");

let pricing = null;
let pack = null; // the chosen pack from /v1/pricing
let currency = "SOL";
// Signed in on /connect in this tab: phone payments credit that wallet, whoever pays.
const signedIn = recallAny();
let forKey = new URLSearchParams(window.location.search).get("for") === "key";
let fullKey = null; // the whole key, when typed or made here: lets the page show the balance
let watch = null; // the purchase being waited for (see watchQuote)
let qrUrl = null; // the QR code's object URL, revoked when replaced

const POLL_MS = 3000;
const POLL_ROUNDS = 60; // about three minutes of waiting while the page is visible

const KEY_RE = /^ufk_([a-z2-7]{10})_[a-z2-7]{52}$/;
const KEY_ID_RE = /^[a-z2-7]{10}$/;
// ISO 3166 alpha-2 codes, named in the visitor's language by Intl.DisplayNames.
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
function ready() {
  if (!countryField.value) return false;
  if (!forKey) return true;
  return Boolean(keyId()) && (keyReveal.hidden || saved.checked);
}

function refreshTarget() {
  keyPanel.hidden = !forKey;
  card.hidden = !(forKey && pricing?.fiat?.available);
  const ok = ready();
  list.querySelectorAll("button").forEach((button) => (button.disabled = !ok));
  qrShow.disabled = !ok;
  if (forKey && !keyId() && keyInput.value.trim()) setStatus(keyStatus, "That isn't an API key or key ID.", "error");
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

document.getElementById("card-pay").addEventListener("click", async (event) => {
  const id = keyId();
  if (!id || !pack || !forKey || (!keyReveal.hidden && !saved.checked)) return refreshTarget();
  event.currentTarget.disabled = true;
  try {
    const started = await api("/v1/credits/fiat-checkout", {
      method: "POST",
      body: JSON.stringify({ key_id: id, checks: pack.checks }),
    });
    window.location.assign(started.checkout_url);
  } catch (error) {
    showMessage("error", error.message || "Card payment isn't available right now.");
    event.currentTarget.disabled = false;
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
      return label;
    }),
  );
  refreshTarget();
}

function renderCountries() {
  let names = null;
  try {
    names = new Intl.DisplayNames([navigator.language, "en"], { type: "region" });
  } catch {
    // older browsers: codes only
  }
  const options = COUNTRIES.map((code) => ({ code, name: names?.of(code) ?? code })).sort((a, b) =>
    a.name.localeCompare(b.name),
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
  refreshTarget();
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

async function showPaid(purchase) {
  qr.hidden = true;
  const headline = `✓ Payment received: ${checksWord(purchase.checks)} added${destination(purchase)}.`;
  const nodes = [element("p", headline, "headline")];
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
      element("p", "Paste the key in the extension's settings (or use it in scripts) and the extension shows your checks. You can close this page."),
    );
  } else {
    nodes.push(element("p", "You can close this page; the extension shows your checks."));
  }
  const balance = element("p", "", "muted");
  nodes.push(balance, button("Buy more", reset));
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
    const quote = await api(`/v1/credits/quotes/${encodeURIComponent(purchase.quoteId)}`, { cache: "no-store" });
    if (watch !== purchase) return;
    if (quote.paid) {
      stopWatching();
      showPaid(purchase);
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
    const image = await response.blob();
    if (qrUrl) URL.revokeObjectURL(qrUrl);
    qrUrl = URL.createObjectURL(new Blob([image], { type: "image/svg+xml" }));
    qr.src = qrUrl;
    qr.hidden = false;
    if (quoteId) {
      watchQuote({ ...info, quoteId });
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
    showMessage("info", `Approve the payment of ${formatPay(purchase.amount)} (${purchase.price.total_usd}) in ${wallet.name}.`);
    bringIntoView();
    await send.signAndSendTransaction({
      account,
      chain: chainOf(pricing.network),
      transaction: fromBase64(purchase.transaction),
    });
    watchQuote({ ...info, quoteId: purchase.quote_id });
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
  }
  renderCountries();
  renderPacks();
  renderWallets([]);
  discoverWallets(renderWallets);
}

start();
