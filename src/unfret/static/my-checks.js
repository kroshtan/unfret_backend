// /my-checks: the shopper's checks on this device, and what to do with them.
//
// Checks live on an Unfret pass (an API key), or, for those who bought with a crypto wallet, on the wallet. This page
// shares the link checker's sign-in (localStorage "unfret-account"), so what it shows is what /check uses:
//   - a pass: how many checks are left, add checks (a top-up naming only the pass's ID), put it on a phone (a QR code
//     holding /check#pass=…, shown only on request), send it to this browser's extension, copy it, or remove it here;
//   - a wallet's sign-in: its checks, add checks with crypto, sign out;
//   - nothing: get checks (/checkout) or paste a pass.
// Always: a lost pass comes back with its backup code (a new pass, same checks), and a wallet can sign in. A backup
// code made on /checkout and not yet confirmed as saved ("unfret-new-key") is shown until its owner confirms.
import { BACKUP_RE, PASS_RE, copyButton, passIdOf, showPassId } from "./pass.js";
import { api, discoverWallets, signIn, walletButton } from "./wallet.js";

const ACCOUNT = "unfret-account"; // the link checker's sign-in
const NEW_KEY = "unfret-new-key"; // a pass made on /checkout whose backup code isn't confirmed saved yet

const $ = (id) => document.getElementById(id);
let account = null; // { kind: "key" | "wallet", token, address, expires_at? }
let restoreAsked = false;

// ── Storage (try/catch: private modes must still work, just without remembering) ──────────────────────────────

function read(name) {
  try {
    return JSON.parse(localStorage.getItem(name) ?? "null");
  } catch {
    return null;
  }
}

function write(name, value) {
  try {
    if (value === null) localStorage.removeItem(name);
    else localStorage.setItem(name, JSON.stringify(value));
  } catch {
    // not remembered: this page still works while it stays open
  }
}

function loadAccount() {
  const stored = read(ACCOUNT);
  if (!stored || typeof stored.token !== "string") return null;
  if (stored.kind === "wallet" && !(Date.parse(stored.expires_at) > Date.now() + 60_000)) {
    write(ACCOUNT, null);
    return null;
  }
  return stored;
}

function usePass(pass) {
  account = { kind: "key", token: pass, address: `key:${passIdOf(pass)}` };
  write(ACCOUNT, account);
}

// A secret shown with its Copy button: the only row it appears in.
function secretRow(row, value, label = "Copy") {
  const code = document.createElement("code");
  code.textContent = value;
  $(row).replaceChildren(code, copyButton(value, label));
}

function checksWord(n) {
  return n === 1 ? "check left" : "checks left";
}

// ── What this device has ─────────────────────────────────────────────────────────────────────────────────────

async function balance(token) {
  const response = await fetch("/v1/credits", { headers: { Authorization: `Bearer ${token}` }, cache: "no-store" });
  if (response.status === 401) return null;
  if (!response.ok) throw new Error("unavailable");
  return (await response.json()).checks_remaining;
}

async function render() {
  account = loadAccount();
  $("mine-pass").hidden = account?.kind !== "key";
  $("mine-wallet").hidden = account?.kind !== "wallet";
  $("mine-none").hidden = Boolean(account);
  $("pass-qr-box").hidden = true;
  // With a working pass here, restoring another one is a side road: a link opens it.
  $("restore").hidden = account?.kind === "key" && !restoreAsked;
  if (!account) return;
  if (account.kind === "key") {
    const id = account.address.slice("key:".length);
    $("pass-id").textContent = showPassId(id);
    $("pass-add").href = `/checkout?mode=topup&key_id=${encodeURIComponent(id)}`;
    secretRow("pass-value-row", account.token, "Copy pass");
  } else {
    $("wallet-address").textContent = `${account.address.slice(0, 4)}…${account.address.slice(-4)}`;
  }
  const [count, word] = account.kind === "key" ? ["pass-checks", "pass-checks-word"] : ["wallet-checks", "wallet-checks-word"];
  try {
    const left = await balance(account.token);
    if (left === null) {
      // Replaced (a backup code, or a purchase picked up in another browser) or a wallet's sign-in that expired.
      write(ACCOUNT, null);
      const restore = $("restore-status");
      restore.textContent =
        account.kind === "key"
          ? "The pass that was on this device no longer works: it was replaced by a new one. Use your backup code, or paste the new pass."
          : "Your wallet's sign-in expired; your checks are still there. Sign in again below.";
      return render();
    }
    $(count).textContent = String(left);
    $(word).textContent = checksWord(left);
  } catch {
    $(count).textContent = "–";
    $(word).textContent = "checks left (couldn't reach Unfret)";
  }
}

// ── A backup code waiting to be saved ───────────────────────────────────────────────────────────────────────

function showUnsavedBackup() {
  const made = read(NEW_KEY);
  const box = $("save-backup");
  if (!made?.funded || !BACKUP_RE.test(made.recovery_code ?? "")) {
    box.hidden = true;
    return;
  }
  secretRow("save-backup-row", made.recovery_code);
  box.hidden = false;
}

$("save-backup-done").addEventListener("click", () => {
  write(NEW_KEY, null);
  $("save-backup").hidden = true;
});

// ── A pass on this device ───────────────────────────────────────────────────────────────────────────────────

$("pass-phone").addEventListener("click", async () => {
  const button = $("pass-phone");
  button.disabled = true;
  try {
    const response = await fetch("/v1/keys/phone-qr", {
      headers: { Authorization: `Bearer ${account.token}` },
      cache: "no-store",
    });
    if (!response.ok) throw new Error("unavailable");
    // A data: URL: the page's policy allows images from here and data: only.
    $("pass-qr").src = `data:image/svg+xml;base64,${btoa(await response.text())}`;
    $("pass-qr-box").hidden = false;
    $("pass-qr-box").scrollIntoView({ block: "nearest" });
  } catch {
    $("restore-status").textContent = "Couldn't make the code right now. Please try again.";
  } finally {
    button.disabled = false;
  }
});

$("pass-qr-hide").addEventListener("click", () => {
  $("pass-qr").removeAttribute("src");
  $("pass-qr-box").hidden = true;
});

// "Send to the extension" opens /checkout?send=pass: the extension (0.11.0 on) takes a pass only from /checkout, so
// the hand-off happens there, with this browser's pass (the link checker's sign-in, which both pages share).

$("pass-remove").addEventListener("click", () => {
  const ok = window.confirm(
    "Remove your pass from this device? Your checks stay on it: to use it again, paste it or scan its code from another device.",
  );
  if (!ok) return;
  write(ACCOUNT, null);
  void render();
});

$("restore-show").addEventListener("click", () => {
  restoreAsked = true;
  $("restore").hidden = false;
  $("restore-input").focus();
});

$("wallet-sign-out").addEventListener("click", () => {
  write(ACCOUNT, null);
  void render();
});

// ── Nothing yet: paste a pass ───────────────────────────────────────────────────────────────────────────────

$("pass-use").addEventListener("click", async () => {
  const status = $("pass-use-status");
  const pass = $("pass-input").value.trim();
  if (!PASS_RE.test(pass)) {
    status.textContent = "That doesn't look like an Unfret pass. Copy it again in full.";
    return;
  }
  status.textContent = "Checking your pass…";
  try {
    if ((await balance(pass)) === null) {
      status.textContent = "This pass no longer works: it was replaced by a new one. Use your backup code below.";
      return;
    }
  } catch {
    status.textContent = "Couldn't reach Unfret. Check your connection.";
    return;
  }
  $("pass-input").value = "";
  status.textContent = "";
  usePass(pass);
  void render();
});

// ── Lost pass: the backup code ──────────────────────────────────────────────────────────────────────────────

$("restore-go").addEventListener("click", async () => {
  const status = $("restore-status");
  const code = $("restore-input").value.trim();
  if (!BACKUP_RE.test(code)) {
    status.textContent = "That doesn't look like a backup code. Copy it again in full.";
    return;
  }
  $("restore-go").disabled = true;
  status.textContent = "Getting your new pass…";
  try {
    const issued = await api("/v1/keys/recover", { method: "POST", body: JSON.stringify({ recovery_code: code }) });
    $("restore-input").value = "";
    // Kept on this device, and its new backup code kept until its owner says it's saved (the box above).
    usePass(issued.key);
    write(NEW_KEY, { key: issued.key, recovery_code: issued.recovery_code, key_id: issued.key_id, funded: true });
    status.textContent = "Here's your new pass, with the same checks. Your backup code is new too: save it now.";
    showUnsavedBackup();
    await render();
    $("save-backup").scrollIntoView({ block: "nearest" });
  } catch (error) {
    status.textContent =
      error.status === 401
        ? "That backup code doesn't work: it may have been used already (each one works once, and you got a new one then)."
        : error.message || "Couldn't get a new pass right now.";
  } finally {
    $("restore-go").disabled = false;
  }
});

// ── A crypto wallet ─────────────────────────────────────────────────────────────────────────────────────────

async function pickWallet(wallet) {
  const status = $("wallet-status");
  status.textContent = `Waiting for ${wallet.name}… Check the wallet for a sign-in request.`;
  try {
    const { session } = await signIn(wallet);
    status.textContent = "";
    account = { kind: "wallet", token: session.token, address: session.address, expires_at: session.expires_at };
    write(ACCOUNT, account);
    $("wallet-sign-in").open = false;
    await render();
  } catch (error) {
    status.textContent = error.message || "Sign-in failed.";
  }
}

function renderWallets(wallets) {
  $("wallets").replaceChildren(...wallets.map((wallet) => walletButton(wallet, pickWallet)));
  $("no-wallets").hidden = wallets.length > 0;
}

// A phone's own browser has no wallet: these reopen this page inside the wallet app's browser.
const here = `${window.location.origin}/my-checks`;
$("open-phantom").href =
  `https://phantom.app/ul/browse/${encodeURIComponent(here)}?ref=${encodeURIComponent(window.location.origin)}`;
$("open-solflare").href =
  `https://solflare.com/ul/v1/browse/${encodeURIComponent(here)}?ref=${encodeURIComponent(window.location.origin)}`;

showUnsavedBackup();
void render();
renderWallets([]);
discoverWallets(renderWallets);
