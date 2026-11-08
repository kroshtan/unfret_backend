// /connect: sign in with a wallet and hand the session to the Veridex browser extension.
// The extension's content script (injected only on this page) receives the window message below, stores the
// session and answers with {type: "veridex-session-received"}.
import { discoverWallets, setStatus, signIn, walletButton } from "./wallet.js";

const list = document.getElementById("wallets");
const empty = document.getElementById("no-wallets");
const status = document.getElementById("status");
const done = document.getElementById("done");
const noExtension = document.getElementById("no-extension");

const HANDOFF_WAIT_MS = 3000;
let handedOver = false;

window.addEventListener("message", (event) => {
  if (event.source !== window || event.origin !== window.location.origin) return;
  if (event.data?.type !== "veridex-session-received") return;
  handedOver = true;
  noExtension.hidden = true;
  done.hidden = false;
  done.focus();
});

function render(wallets) {
  list.replaceChildren(...wallets.map((wallet) => walletButton(wallet, connect)));
  empty.hidden = wallets.length > 0;
}

async function connect(wallet) {
  setStatus(status, `Waiting for ${wallet.name}… Check the wallet for a sign-in request.`);
  list.querySelectorAll("button").forEach((button) => (button.disabled = true));
  try {
    const { session } = await signIn(wallet);
    setStatus(status, `Signed in as ${session.address.slice(0, 4)}…${session.address.slice(-4)}.`, "ok");
    window.postMessage(
      { type: "veridex-session", token: session.token, address: session.address, expires_at: session.expires_at },
      window.location.origin,
    );
    setTimeout(() => {
      if (!handedOver) noExtension.hidden = false;
    }, HANDOFF_WAIT_MS);
  } catch (error) {
    setStatus(status, error.message || "Sign-in failed.", "error");
  } finally {
    list.querySelectorAll("button").forEach((button) => (button.disabled = false));
  }
}

render([]);
discoverWallets(render);
