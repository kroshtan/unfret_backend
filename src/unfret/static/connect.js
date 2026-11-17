// /connect: sign in with a wallet and hand the session to the Unfret browser extension.
// Two ways in:
// - Same browser: the extension's content script (injected only on this page) receives the window message below,
//   stores the session and answers with {type: "veridex-session-received"}. Both message types keep their names
//   from before the rename to Unfret: released extensions (up to 0.8.3) send and expect exactly these.
// - Phone pairing (/connect?pair=<id>, opened from the extension's QR code in a phone wallet's browser): the
//   message names the pairing's confirmation code, the server attaches the session to the pairing and the extension
//   collects it; this page keeps nothing.
// From extension 0.12.0, when it doesn't sign in at once it answers {type: "unfret-session-status", status}:
// "needs-click" (it takes a first sign-in only right after a click here, or in a tab it opened itself: a button hands
// the session over again), "confirm" (it uses another pass or wallet and asks in its own window; the received message
// follows on a yes) or "kept" (the user said no there). Older extensions never send it.
// After a same-browser sign-in the page offers "Have an invite?": a code someone gave the user, redeemed for this
// (new) wallet with the session it just got.
import { INVITE_RE, NOT_A_CODE, api, discoverWallets, redeemInvite, setStatus, signIn, walletButton } from "./wallet.js";

const list = document.getElementById("wallets");
const empty = document.getElementById("no-wallets");
const status = document.getElementById("status");
const done = document.getElementById("done");
const noExtension = document.getElementById("no-extension");
const pairing = document.getElementById("pairing");
const pairCode = document.getElementById("pair-code");
const pairError = document.getElementById("pair-error");
const pairDone = document.getElementById("pair-done");
const invite = document.getElementById("invite");
const handoff = document.getElementById("handoff");
let signedIn = null; // this tab's sign-in, for redeeming an invite

const HANDOFF_WAIT_MS = 3000;
const pairId = new URLSearchParams(window.location.search).get("pair");
let handedOver = false;
let pairReady = !pairId;

function postSession(session) {
  window.postMessage(
    { type: "veridex-session", token: session.token, address: session.address, expires_at: session.expires_at },
    window.location.origin,
  );
}

function sayHandoff(...nodes) {
  handoff.replaceChildren(...nodes);
  handoff.hidden = nodes.length === 0;
}

function paragraph(text) {
  const p = document.createElement("p");
  p.textContent = text;
  return p;
}

// The extension's answer when it didn't sign in at once (0.12.0 on).
function extensionStatus(status) {
  handedOver = true;
  noExtension.hidden = true;
  if (status === "needs-click" && signedIn) {
    const send = document.createElement("button");
    send.type = "button";
    send.className = "primary";
    send.textContent = "Send it to the extension";
    send.addEventListener("click", () => {
      send.disabled = true;
      postSession(signedIn);
    });
    sayHandoff(paragraph("Sign your Unfret extension in with this wallet?"), send);
  } else if (status === "confirm") {
    sayHandoff(paragraph("Confirm in the Unfret window that just opened: it names this wallet and the account it uses now."));
  } else if (status === "kept") {
    sayHandoff(paragraph("Your Unfret extension keeps what it has. Sign in here again any time to switch it to this wallet."));
  }
}

window.addEventListener("message", (event) => {
  if (pairId || event.source !== window || event.origin !== window.location.origin) return;
  if (event.data?.type === "unfret-session-status") return extensionStatus(event.data.status);
  if (event.data?.type !== "veridex-session-received") return;
  handedOver = true;
  noExtension.hidden = true;
  sayHandoff();
  done.hidden = false;
  done.focus();
});

function setButtons(enabled) {
  list.querySelectorAll("button").forEach((button) => (button.disabled = !enabled));
}

// A phone camera usually opens the system browser, which has no wallet. Phantom's and Solflare's universal links
// reopen this exact page inside the wallet app's own browser, where the wallet is available.
const openInWallet = document.getElementById("open-in-wallet");
document.getElementById("open-phantom").href =
  `https://phantom.app/ul/browse/${encodeURIComponent(window.location.href)}?ref=${encodeURIComponent(window.location.origin)}`;
document.getElementById("open-solflare").href =
  `https://solflare.com/ul/v1/browse/${encodeURIComponent(window.location.href)}?ref=${encodeURIComponent(window.location.origin)}`;

function render(wallets) {
  list.replaceChildren(...wallets.map((wallet) => walletButton(wallet, connect)));
  empty.hidden = wallets.length > 0;
  openInWallet.hidden = wallets.length > 0 || !pairId;
  setButtons(pairReady);
}

function pairFailed(message) {
  pairing.hidden = true;
  pairError.textContent = message;
  pairError.hidden = false;
  pairReady = false;
  setButtons(false);
}

async function loadPairing() {
  try {
    const info = await api(`/v1/auth/pair/${encodeURIComponent(pairId)}/info`);
    pairCode.textContent = info.confirm_code;
    pairing.hidden = false;
    pairReady = true;
    setButtons(true);
  } catch (error) {
    if (error.status === 410) pairFailed("This sign-in request expired. Start again on your computer.");
    else if (error.status === 409) pairFailed("This sign-in request was already used.");
    else pairFailed(error.message || "This sign-in request isn't valid. Start again on your computer.");
  }
}

async function connect(wallet) {
  setStatus(status, `Waiting for ${wallet.name}… Check the wallet for a sign-in request.`);
  setButtons(false);
  try {
    const { session } = await signIn(wallet, { pairId });
    if (pairId) {
      // The session now waits for the computer; this page discards it.
      setStatus(status, "", "ok");
      pairing.hidden = true;
      list.hidden = true;
      pairReady = false;
      pairDone.hidden = false;
      pairDone.focus();
      return;
    }
    setStatus(status, `Connected ${session.address.slice(0, 4)}…${session.address.slice(-4)}.`, "ok");
    signedIn = session;
    invite.hidden = false;
    postSession(session);
    setTimeout(() => {
      if (!handedOver) noExtension.hidden = false;
    }, HANDOFF_WAIT_MS);
  } catch (error) {
    setStatus(status, error.message || "Sign-in failed.", "error");
  } finally {
    setButtons(pairReady);
  }
}

document.getElementById("invite-redeem").addEventListener("click", async (event) => {
  const inviteStatus = document.getElementById("invite-status");
  const input = document.getElementById("invite-input");
  const code = input.value.trim().toLowerCase();
  if (!INVITE_RE.test(code)) return setStatus(inviteStatus, NOT_A_CODE, "error");
  if (!signedIn) return setStatus(inviteStatus, "Connect a wallet first.", "error");
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const added = await redeemInvite(code, signedIn.token);
    input.value = "";
    setStatus(inviteStatus, `✓ ${added.checks_added} checks added to this wallet.`, "ok");
  } catch (error) {
    setStatus(inviteStatus, error.message || "That code didn't work.", "error");
  } finally {
    button.disabled = false;
  }
});

render([]);
discoverWallets(render);
if (pairId) loadPairing();
