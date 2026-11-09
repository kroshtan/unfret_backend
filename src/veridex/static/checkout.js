// /checkout: buy credits with SOL or USDC. The wallet signs and sends a transfer to Veridex's hot wallet; the server
// credits the sending wallet once the transfer is final (SOL at its dollar value then). Phone wallets can pay the same
// request by QR code. Prices are in US dollars: USDC pays them exactly, SOL at the live quote in /v1/pricing.
// Credits can go to an API key instead (?for=key): a new one (made here after a small proof of work), an existing
// one, or one recovered with its recovery code. The memo then names the key's public ID; the key itself never leaves
// this page except to check its balance.
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
const custom = document.getElementById("custom-sol");
const summary = document.getElementById("summary");
const list = document.getElementById("wallets");
const empty = document.getElementById("no-wallets");
const qr = document.getElementById("qr");
const qrLink = document.getElementById("qr-link");
const shop = document.getElementById("shop");
const unavailable = document.getElementById("unavailable");
const currencyField = document.getElementById("currency");
const usdcNote = document.getElementById("usdc-note");
const keyPanel = document.getElementById("key-panel");
const keyInput = document.getElementById("key-input");
const keyStatus = document.getElementById("key-status");
const keyReveal = document.getElementById("key-reveal");
const saved = document.getElementById("saved");
const card = document.getElementById("card");

let pricing = null;
let usd = 0; // what is being bought, in micro-dollars (1 USD = 1,000,000)
let currency = "SOL";
// Signed in on /connect in this tab: phone payments credit that wallet (a memo), whoever pays.
const signedIn = recallAny();
let forKey = new URLSearchParams(window.location.search).get("for") === "key";
let fullKey = null; // the whole key, when typed or made here: lets the page show the balance

const KEY_RE = /^ufk_([a-z2-7]{10})_[a-z2-7]{52}$/;
const KEY_ID_RE = /^[a-z2-7]{10}$/;

// The key ID to credit, or null. A whole key is reduced to its ID before anything is sent for a purchase.
function keyId() {
  const value = keyInput.value.trim();
  const match = KEY_RE.exec(value);
  fullKey = match ? value : null;
  if (match) return match[1];
  return KEY_ID_RE.test(value) ? value : null;
}

// Paying needs a target: the paying wallet, or a valid key (and a new key's codes saved first).
function ready() {
  if (!forKey) return true;
  return Boolean(keyId()) && (keyReveal.hidden || saved.checked);
}

function refreshTarget() {
  keyPanel.hidden = !forKey;
  card.hidden = !(forKey && pricing?.fiat?.available);
  const ok = ready();
  list.querySelectorAll("button").forEach((button) => (button.disabled = !ok));
  qrLink.hidden = !ok;
  if (forKey && !keyId() && keyInput.value.trim()) setStatus(keyStatus, "That isn't an API key or key ID.", "error");
  else if (forKey && !keyReveal.hidden && !saved.checked) setStatus(keyStatus, "Save both codes, then tick the box.");
  else setStatus(keyStatus, "");
  if (usd) choose(usd);
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
  if (!id || !ready()) return refreshTarget();
  event.currentTarget.disabled = true;
  try {
    const amount = Math.max(usd, pricing.fiat.min_usd_micros);
    const started = await api("/v1/credits/fiat-checkout", {
      method: "POST",
      body: JSON.stringify({ key_id: id, amount_usd_micros: Math.round(amount / 10000) * 10000 }),
    });
    window.location.assign(started.checkout_url);
  } catch (error) {
    setStatus(status, error.message || "Card payment isn't available right now.", "error");
    event.currentTarget.disabled = false;
  }
});

function bonusFor(value) {
  const pct = Math.max(0, ...pricing.bonuses.filter((b) => value >= b.min_usd_micros).map((b) => b.bonus_pct));
  return { pct, credited: value + Math.floor((value * pct) / 100) };
}

function formatUsd(micros) {
  return `$${(micros / 1e6).toFixed(2)}`;
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

function checksWord(n) {
  return `${n} ${n === 1 ? "check" : "checks"}`;
}

function choose(value) {
  usd = value;
  const amount = payAmount(value);
  const { pct, credited } = bonusFor(value);
  const checks = Math.floor(credited / pricing.analysis_usd_micros);
  const paid = currency === "SOL" ? `${formatUsd(value)} (${formatPay(amount)})` : formatPay(amount);
  summary.textContent = `${paid} buys ${checksWord(checks)}` + (pct ? ` (includes a ${pct}% bonus).` : ".");
  let url;
  let src = `/v1/credits/solana-pay.svg?currency=${currency}&amount=${amount}`;
  if (currency === "SOL") {
    const sol = (amount / 1e9).toFixed(9).replace(/\.?0+$/, "");
    url = `solana:${pricing.hot_wallet}?amount=${sol}&label=Veridex&message=Veridex%20credits`;
  } else {
    const usdc = (amount / 1e6).toFixed(6).replace(/\.?0+$/, "");
    url = `solana:${pricing.hot_wallet}?amount=${usdc}&spl-token=${pricing.usdc.mint}&label=Veridex&message=Veridex%20credits`;
  }
  const id = forKey ? keyId() : null;
  if (id) {
    url += `&memo=${encodeURIComponent(`veridex:credit:key:${id}`)}`;
    src += `&key_id=${id}`;
  } else if (signedIn && !forKey) {
    url += `&memo=${encodeURIComponent(`veridex:credit:${signedIn.address}`)}`;
    src += `&beneficiary=${encodeURIComponent(signedIn.address)}`;
  }
  if (forKey && !id) {
    qr.hidden = true;
    return;
  }
  qr.src = src;
  qr.hidden = false;
  qrLink.href = url;
}

function renderPacks() {
  // ?checks=<n> preselects a pack, or asks for exactly n analyses when no pack matches.
  const preset = Number(new URLSearchParams(window.location.search).get("checks")) || 0;
  const match = pricing.suggested_packs.find((pack) => pack.checks === preset);
  const selected = match ?? (preset ? null : pricing.suggested_packs[0]);
  packs.replaceChildren(
    ...pricing.suggested_packs.map((pack) => {
      const label = document.createElement("label");
      label.className = "pack";
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "pack";
      input.value = String(pack.usd_micros);
      input.checked = pack === selected;
      input.addEventListener("change", () => choose(pack.usd_micros));
      const sol = currency === "SOL" ? ` ≈ ${formatPay(payAmount(pack.usd_micros))}` : "";
      label.append(input, ` ${checksWord(pack.checks)} · ${formatUsd(pack.usd_micros)}${sol}`);
      return label;
    }),
  );
  if (selected) choose(selected.usd_micros);
  else if (preset) choose(preset * pricing.analysis_usd_micros);
}

custom.addEventListener("input", () => {
  const value = Number(custom.value);
  if (value > 0) {
    packs.querySelectorAll("input").forEach((input) => (input.checked = false));
    // Typed in dollars, whatever the currency.
    choose(Math.round(value * 1e6));
  }
});

currencyField.addEventListener("change", (event) => {
  currency = event.target.value === "SOL" && pricing.sol ? "SOL" : "USDC";
  custom.value = "";
  renderPacks();
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
  if (!usd || !ready()) return;
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
    const amount = payAmount(usd);
    const target = forKey ? { key_id: keyId() } : {};
    const purchase = await api("/v1/credits/purchase-tx", {
      method: "POST",
      body: JSON.stringify({ address: account.address, currency, amount, ...target }),
    });
    setStatus(status, `Approve the payment of ${formatPay(amount)} in ${wallet.name}.`);
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
  custom.min = String(pricing.purchase.min_usd_micros / 1e6);
  document.getElementById("beneficiary").querySelector(`input[value="${forKey ? "key" : "wallet"}"]`).checked = true;
  if (pricing.fiat?.available) document.getElementById("card-min").textContent = formatUsd(pricing.fiat.min_usd_micros);
  if (new URLSearchParams(window.location.search).get("paid")) {
    setStatus(status, "Thanks! Your card payment is being confirmed; the credits reach your key in a minute.", "ok");
  }
  renderPacks();
  renderWallets([]);
  discoverWallets(renderWallets);
}

start();
