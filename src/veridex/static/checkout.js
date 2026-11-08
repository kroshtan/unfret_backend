// /checkout: buy credits with SOL. The wallet signs and sends a plain transfer to Veridex's hot wallet; the server
// credits the sending wallet once the transfer is final. Phone wallets can pay the same request by QR code.
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

let pricing = null;
let lamports = 0;
// Signed in on /connect in this tab: phone payments credit that wallet (a memo), whoever pays.
const signedIn = recallAny();

function bonusFor(amount) {
  const pct = Math.max(0, ...pricing.bonuses.filter((b) => amount >= b.min_lamports).map((b) => b.bonus_pct));
  return { pct, credited: amount + Math.floor((amount * pct) / 100) };
}

function choose(amount) {
  lamports = amount;
  const { pct, credited } = bonusFor(amount);
  const checks = Math.floor(credited / pricing.analysis_lamports);
  summary.textContent =
    `${formatSol(amount)} buys ${checks} ${checks === 1 ? "analysis" : "analyses"}` + (pct ? ` (includes a ${pct}% bonus).` : ".");
  const sol = (amount / 1e9).toFixed(9).replace(/\.?0+$/, "");
  let url = `solana:${pricing.hot_wallet}?amount=${sol}&label=Veridex&message=Veridex%20credits`;
  let src = `/v1/credits/solana-pay.svg?lamports=${amount}`;
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
      label.append(input, ` ${pack.checks} analyses · ${formatSol(pack.lamports)}`);
      return label;
    }),
  );
  if (selected) choose(selected.lamports);
  else if (preset) choose(preset * pricing.analysis_lamports);
}

custom.addEventListener("input", () => {
  const sol = Number(custom.value);
  if (sol > 0) {
    packs.querySelectorAll("input").forEach((input) => (input.checked = false));
    choose(Math.round(sol * 1e9));
  }
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
    const purchase = await api("/v1/credits/purchase-tx", {
      method: "POST",
      body: JSON.stringify({ address: account.address, lamports }),
    });
    setStatus(status, `Approve the payment of ${formatSol(lamports)} in ${wallet.name}.`);
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
  renderPacks();
  renderWallets([]);
  discoverWallets(renderWallets);
}

start();
