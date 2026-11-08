// Wallet Standard discovery and Sign-In With Solana, without dependencies or third-party requests.
// Wallets announce themselves on the `wallet-standard:register-wallet` event; the page announces itself with
// `wallet-standard:app-ready`. Both carry the same `{ register }` API.

const SESSION_KEY = "veridex-session";

export function discoverWallets(onChange) {
  const wallets = new Set();
  const api = Object.freeze({
    register(...items) {
      for (const wallet of items) wallets.add(wallet);
      onChange([...wallets].filter(isSolanaWallet));
      return () => {
        for (const wallet of items) wallets.delete(wallet);
        onChange([...wallets].filter(isSolanaWallet));
      };
    },
  });
  window.addEventListener("wallet-standard:register-wallet", (event) => {
    try {
      event.detail(api);
    } catch (error) {
      console.error("wallet registration failed", error);
    }
  });
  window.dispatchEvent(new CustomEvent("wallet-standard:app-ready", { detail: api }));
}

function isSolanaWallet(wallet) {
  return (
    Array.isArray(wallet.chains) &&
    wallet.chains.some((chain) => chain.startsWith("solana:")) &&
    Boolean(wallet.features?.["standard:connect"])
  );
}

export function walletButton(wallet, onPick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "wallet";
  if (typeof wallet.icon === "string" && wallet.icon.startsWith("data:image/")) {
    const icon = document.createElement("img");
    icon.src = wallet.icon;
    icon.alt = "";
    icon.width = 28;
    icon.height = 28;
    button.append(icon);
  }
  button.append(document.createTextNode(wallet.name));
  button.addEventListener("click", () => onPick(wallet));
  return button;
}

export function toBase64(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

export function fromBase64(text) {
  return Uint8Array.from(atob(text), (char) => char.charCodeAt(0));
}

export function chainOf(network) {
  return network === "solana-mainnet" ? "solana:mainnet" : "solana:devnet";
}

// The canonical sign-in text (the Wallet Standard createSignInMessageText layout, without the optional chain-id line);
// the server parses what was signed.
export function signInText(fields, address) {
  return [
    `${fields.domain} wants you to sign in with your Solana account:`,
    address,
    "",
    fields.statement,
    "",
    `URI: ${fields.uri}`,
    `Version: ${fields.version}`,
    `Nonce: ${fields.nonce}`,
    `Issued At: ${fields.issued_at}`,
    `Expiration Time: ${fields.expiration_time}`,
  ].join("\n");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers ?? {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.detail ?? `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return body;
}

export { api };

// Sign in with a wallet: one signature over a readable message, which grants no spending rights.
// With pairId (phone sign-in for a computer's extension) the message names the pairing's confirmation code and the
// session goes to the pairing, not to this tab. Returns { session, account }.
export async function signIn(wallet, { pairId } = {}) {
  const fields = await api(pairId ? `/v1/auth/nonce?pair=${encodeURIComponent(pairId)}` : "/v1/auth/nonce");
  let account;
  let signedMessage;
  let signature;
  const signInFeature = wallet.features["solana:signIn"];
  if (signInFeature) {
    const [output] = await signInFeature.signIn({
      domain: fields.domain,
      statement: fields.statement,
      uri: fields.uri,
      version: fields.version,
      // The chain ID is left out on purpose: wallets refuse the request when it doesn't match their selected network exactly (and differ
      // on "devnet" vs "solana:devnet"). The nonce already binds the sign-in to this site and a 5-minute window.
      nonce: fields.nonce,
      issuedAt: fields.issued_at,
      expirationTime: fields.expiration_time,
    });
    ({ account, signedMessage, signature } = output);
  } else {
    const signMessage = wallet.features["solana:signMessage"];
    if (!signMessage) throw new Error(`${wallet.name} can't sign messages.`);
    const { accounts } = await wallet.features["standard:connect"].connect();
    account = accounts[0];
    if (!account) throw new Error("No account was shared by the wallet.");
    const message = new TextEncoder().encode(signInText(fields, account.address));
    const [output] = await signMessage.signMessage({ account, message });
    signedMessage = output.signedMessage ?? message;
    signature = output.signature;
  }
  const session = await api("/v1/auth/verify", {
    method: "POST",
    body: JSON.stringify({
      address: account.address,
      signed_message: toBase64(signedMessage),
      signature: toBase64(signature),
      ...(pairId ? { pair_id: pairId } : {}),
    }),
  });
  if (!pairId) remember(session);
  return { session, account };
}

// The session is kept for this tab only, so /checkout can reuse a sign-in from /connect.
export function remember(session) {
  try {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
  } catch {
    // storage unavailable (private mode): signing in again works too
  }
}

export function recall(address) {
  try {
    const session = JSON.parse(sessionStorage.getItem(SESSION_KEY) ?? "null");
    if (session && session.address === address && Date.parse(session.expires_at) > Date.now() + 60_000) return session;
  } catch {
    // ignore unreadable storage
  }
  return null;
}

// Any still-valid session in this tab, whichever wallet it belongs to.
export function recallAny() {
  try {
    const session = JSON.parse(sessionStorage.getItem(SESSION_KEY) ?? "null");
    if (session && Date.parse(session.expires_at) > Date.now() + 60_000) return session;
  } catch {
    // ignore unreadable storage
  }
  return null;
}

export function formatSol(lamports) {
  return `${(lamports / 1e9).toLocaleString(undefined, { maximumFractionDigits: 9 })} SOL`;
}

export function setStatus(element, text, kind = "info") {
  element.textContent = text;
  element.dataset.kind = kind;
}
