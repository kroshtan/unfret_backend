// /checkout: buy credits with SOL or USDC. The wallet signs and sends a transfer to Veridex's hot wallet; the server
// credits the sending wallet once the transfer is final (USDC at its SOL value then). Phone wallets can pay the same
// request by QR code. Prices are set in SOL; the USDC amounts come from the live quote in /v1/pricing.
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
const customUnit = document.getElementById("custom-unit");

let pricing = null;
let lamports = 0; // what is being bought, by SOL value
let currency = "SOL";
// Signed in on /connect in this tab: phone payments credit that wallet (a memo), whoever pays.
const signedIn = recallAny();

function bonusFor(amount) {
  const pct = Math.max(0, ...pricing.bonuses.filter((b) => amount >= b.min_lamports).map((b) => b.bonus_pct));
  return { pct, credited: amount + Math.floor((amount * pct) / 100) };
}

// The amount to pay in the chosen currency's base units, for a SOL value (USDC rounded up, as the server prices it).
function payAmount(value) {
  if (currency === "SOL") return value;
  return Math.ceil((value * pricing.usdc.analysis_units) / pricing.analysis_lamports);
}

function formatPay(amount) {
  return currency === "SOL" ? formatSol(amount) : `${(amount / 1e6).toFixed(2)} USDC`;
}

function choose(value) {
  lamports = value;
  const amount = payAmount(value);
  const { pct, credited } = bonusFor(value);
  const checks = Math.floor(credited / pricing.analysis_lamports);
  summary.textContent =
    `${formatPay(amount)} buys ${checks} ${checks === 1 ? "analysis" : "analyses"}` + (pct ? ` (includes a ${pct}% bonus).` : ".");
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
      input.value = String(pack.lamports);
      input.checked = pack === selected;
      input.addEventListener("change", () => choose(pack.lamports));
      label.append(input, ` ${pack.checks} analyses · ${formatPay(payAmount(pack.lamports))}`);
      return label;
    }),
  );
  if (selected) choose(selected.lamports);
  else if (preset) choose(preset * pricing.analysis_lamports);
}

custom.addEventListener("input", () => {
  const value = Number(custom.value);
  if (value > 0) {
    packs.querySelectorAll("input").forEach((input) => (input.checked = false));
    // Typed in the chosen currency; kept internally by SOL value.
    const asLamports =
      currency === "SOL"
        ? Math.round(value * 1e9)
        : Math.floor((value * 1e6 * pricing.analysis_lamports) / pricing.usdc.analysis_units);
    choose(asLamports);
  }
});

currencyField.addEventListener("change", (event) => {
  currency = event.target.value === "USDC" && pricing.usdc ? "USDC" : "SOL";
  customUnit.textContent = currency;
  custom.value = "";
  renderPacks();
});

async function waitForCredits(token, before) {
  for (let attempt = 0; attempt < 36; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 5000));
    try {
      const credits = await api("/v1/credits", { headers: { Authorization: `Bearer ${token}` } });
      if (credits.balance_lamports > before) return credits;
    } catch {
      // keep polling
    }
  }
  return null;
}

async function buy(wallet) {
  if (!lamports) return;
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
    const before = (await api("/v1/credits", { headers: { Authorization: `Bearer ${session.token}` } })).balance_lamports;
    const amount = payAmount(lamports);
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
      setStatus(status, `Done: you have ${credits.checks_remaining} analyses (${formatSol(credits.balance_lamports)}).`, "ok");
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
  const usdcRadio = currencyField.querySelector('input[value="USDC"]');
  if (pricing.usdc) {
    const rate = pricing.usdc.sol_usd ? ` at ${Number(pricing.usdc.sol_usd).toFixed(2)} USD/SOL` : "";
    usdcNote.textContent = `USDC amounts are the SOL prices converted${rate}.`;
  } else {
    usdcRadio.disabled = true;
    usdcNote.textContent = "USDC is unavailable right now (no fresh price); pay in SOL.";
  }
  renderPacks();
  renderWallets([]);
  discoverWallets(renderWallets);
}

start();
