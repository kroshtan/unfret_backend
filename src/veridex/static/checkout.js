// /checkout: buy credits with SOL or USDC. The wallet signs and sends a transfer to Veridex's hot wallet; the server
// credits the sending wallet once the transfer is final (SOL at its dollar value then). Phone wallets can pay the same
// request by QR code. Prices are in US dollars: USDC pays them exactly, SOL at the live quote in /v1/pricing.
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

let pricing = null;
let usd = 0; // what is being bought, in micro-dollars (1 USD = 1,000,000)
let currency = "SOL";
// Signed in on /connect in this tab: phone payments credit that wallet (a memo), whoever pays.
const signedIn = recallAny();

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
  if (signedIn) {
    url += `&memo=${encodeURIComponent(`veridex:credit:${signedIn.address}`)}`;
    src += `&beneficiary=${encodeURIComponent(signedIn.address)}`;
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

async function buy(wallet) {
  if (!usd) return;
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
    let session = recall(account.address);
    if (!session) {
      setStatus(status, "Sign in first, so this page can show when your credits arrive (no spending rights).");
      ({ session } = await signIn(wallet));
    }
    const balance = await api("/v1/credits", { headers: { Authorization: `Bearer ${session.token}` } });
    const before = balance.balance_usd_micros ?? balance.balance_lamports;
    const amount = payAmount(usd);
    const purchase = await api("/v1/credits/purchase-tx", {
      method: "POST",
      body: JSON.stringify({ address: account.address, currency, amount }),
    });
    setStatus(status, `Approve the payment of ${formatPay(amount)} in ${wallet.name}.`);
    await send.signAndSendTransaction({
      account,
      chain: chainOf(pricing.network),
      transaction: fromBase64(purchase.transaction),
    });
    setStatus(status, "Payment sent. Credits arrive once it is final, usually within 30 seconds…");
    const credits = await waitForCredits(session.token, before);
    if (credits) {
      setStatus(status, `Done: you have ${checksWord(credits.checks_remaining)} (${formatUsd(credits.balance_usd_micros)}).`, "ok");
    } else {
      setStatus(status, "The payment is still settling. Your credits will appear in the extension shortly.", "info");
    }
  } catch (error) {
    setStatus(status, error.message || "The purchase didn't go through.", "error");
  } finally {
    list.querySelectorAll("button").forEach((button) => (button.disabled = false));
  }
}

function renderWallets(wallets) {
  list.replaceChildren(...wallets.map((wallet) => walletButton(wallet, buy)));
  empty.hidden = wallets.length > 0;
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
  renderPacks();
  renderWallets([]);
  discoverWallets(renderWallets);
}

start();
