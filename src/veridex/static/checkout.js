// /checkout: buy a pack of credits with SOL or USDC (or, for API keys, by card). The wallet signs and sends a transfer
// to Unfret's hot wallet; the server credits the pack once the transfer is final. Phone wallets can pay the same
// request by QR code. Prices come from /v1/pricing, all derived from the price of one check: each pack has a price
// (by card) and a lower crypto price, plus VAT for the buyer's country (prefilled from where the request comes from).
// Credits can go to an API key instead (?for=key): a new one (made here after a small proof of work), an existing
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
  signIn,
  walletButton,
} from "./wallet.js";

const status = document.getElementById("status");
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
  qr.hidden = true;
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
    setStatus(status, error.message || "Card payment isn't available right now.", "error");
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
  if (!pricing || !pack) return;
  document.getElementById("card-price").textContent = formatUsd(pack.price_usd_micros);
  document.getElementById("card-tax").textContent = "plus sales tax/VAT where applicable, added at checkout";
  if (!countryField.value) {
    summary.textContent = `${checksWord(pack.checks)}: choose your country to see the crypto price with VAT.`;
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
      label.append(input, ` ${option.label.en}`);
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

qrShow.addEventListener("click", () => {
  if (!ready() || !pack) return refreshTarget();
  const params = new URLSearchParams({ checks: String(pack.checks), country: countryField.value, currency });
  for (const [name, value] of Object.entries(purchaseTarget())) params.set(name, value);
  qr.src = `/v1/credits/solana-pay.svg?${params}`;
  qr.hidden = false;
});

async function waitForCredits(token, before) {
  for (let attempt = 0; attempt < 36; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 5000));
    try {
      const credits = await api("/v1/credits", { headers: { Authorization: `Bearer ${token}` } });
      if ((credits.balance_usd_micros ?? credits.balance_lamports) > before) return credits;
    } catch {
      // keep polling
    }
  }
  return null;
}

async function balanceOf(token) {
  const credits = await api("/v1/credits", { headers: { Authorization: `Bearer ${token}` } });
  return credits.balance_usd_micros ?? credits.balance_lamports;
}

async function buy(wallet) {
  if (!pack || !ready()) return;
  const send = wallet.features["solana:signAndSendTransaction"];
  if (!send) {
    setStatus(status, `${wallet.name} can't send transactions from this page; use the QR code instead.`, "error");
    return;
  }
  list.querySelectorAll("button").forEach((button) => (button.disabled = true));
  try {
    const { accounts } = await wallet.features["standard:connect"].connect();
    const account = accounts[0];
    if (!account) throw new Error("No account was shared by the wallet.");
    // Paying for a key needs no sign-in; its balance shows only when the whole key is on this page.
    let token = forKey ? fullKey : null;
    if (!forKey) {
      let session = recall(account.address);
      if (!session) {
        setStatus(status, "Sign in first, so this page can show when your credits arrive (no spending rights).");
        ({ session } = await signIn(wallet));
      }
      token = session.token;
    }
    const before = token ? await balanceOf(token) : 0;
    const target = forKey ? { key_id: keyId() } : {};
    const purchase = await api("/v1/credits/purchase-tx", {
      method: "POST",
      body: JSON.stringify({
        address: account.address,
        currency,
        checks: pack.checks,
        country: countryField.value,
        ...target,
      }),
    });
    setStatus(status, `Approve the payment of ${formatPay(purchase.amount)} (${purchase.price.total_usd}) in ${wallet.name}.`);
    await send.signAndSendTransaction({
      account,
      chain: chainOf(pricing.network),
      transaction: fromBase64(purchase.transaction),
    });
    setStatus(status, "Payment sent. Credits arrive once it is final, usually within 30 seconds…");
    if (!token) {
      setStatus(status, "Payment sent. The credits reach your key once it is final, usually within 30 seconds.", "ok");
      return;
    }
    const credits = await waitForCredits(token, before);
    if (credits) {
      setStatus(status, `Done: you have ${checksWord(credits.checks_remaining)} (${formatUsd(credits.balance_usd_micros)}).`, "ok");
    } else {
      setStatus(status, "The payment is still settling. Your credits will appear in the extension shortly.", "info");
    }
  } catch (error) {
    setStatus(status, error.message || "The purchase didn't go through.", "error");
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
    setStatus(status, "Thanks! Your card payment is being confirmed; the credits reach your key in a minute.", "ok");
  }
  renderCountries();
  renderPacks();
  renderWallets([]);
  discoverWallets(renderWallets);
}

start();
