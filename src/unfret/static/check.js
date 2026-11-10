// /check: check a product link from any browser, phones first. The link goes to POST /v1/analyze/stream with only
// a URL; the server fetches the page itself and streams progress, then the result. A page it can't check that way
// (the shop blocks automatic visitors, a bot check, a page that needs a browser) comes back as a free 422, shown as
// such and never as a verdict.
//
// What this page keeps, in localStorage: only the sign-in (a wallet's session token or an API key) until sign-out.
// No history: the link lives in the text field and in memory for the one check (and goes into a shared result
// only when the shopper shares it, after a preview). A shared link (/check?url=… or,
// via the service worker, /check#url=…) is read here and removed from the address bar at once.
import {
  BAND_SYMBOL,
  BAND_WORD,
  CHECKLIST,
  SseParser,
  bandOf,
  checklistDone,
  extractLink,
  sharedLink,
} from "./check-core.js";
import { el, resultNodes } from "./check-render.js";
import { discoverWallets, signIn, walletButton } from "./wallet.js";

const STORE_KEY = "unfret-account";
const KEY_RE = /^ufk_([a-z2-7]{10})_[a-z2-7]{52}$/;
const UNREADABLE = {
  PAGE_BLOCKED: {
    heading: "This shop blocks automatic checks",
    body: "The shop refused Unfret's server, or showed it a bot check instead of the page. Unfret won't guess from half a page, so there is no verdict.",
  },
  PAGE_TOO_THIN: {
    heading: "This page needs a browser to show itself",
    body: "Without a browser the shop sends an almost empty page, too little to judge. If this was a search or home page, try the link to the product itself.",
  },
  PAGE_UNAVAILABLE: {
    heading: "Couldn't open this page",
    body: "Unfret's server couldn't load it: the address may be wrong, the page gone or the shop down. Check the link and try again.",
  },
};

const $ = (id) => document.getElementById(id);
const form = $("form");
const field = $("link");
const pasteButton = $("paste");
const checkButton = $("check");

let account = null; // { kind: "wallet" | "key", token, address, expires_at }
let expired = false;
let checks = null;
let pricing = null;
let running = null; // the AbortController of the check in progress
let last = null; // { body, result } of the check on screen, for a report or a share
let preview = null; // what sharing the check on screen would make public

// ── Storage (try/catch: private modes and blocked storage must still work, just without remembering) ───────

function loadAccount() {
  try {
    const stored = JSON.parse(localStorage.getItem(STORE_KEY) ?? "null");
    if (!stored || typeof stored.token !== "string") return null;
    if (stored.kind === "wallet" && !(Date.parse(stored.expires_at) > Date.now() + 60_000)) {
      expired = true;
      forgetAccount();
      return null;
    }
    return stored;
  } catch {
    return null;
  }
}

function saveAccount(value) {
  account = value;
  expired = false;
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(value));
  } catch {
    // not remembered: this tab still works
  }
}

function forgetAccount() {
  try {
    localStorage.removeItem(STORE_KEY);
  } catch {
    // nothing was stored
  }
}

// ── Network ──────────────────────────────────────────────────────────────────────────────────────────────

function headers(json) {
  return {
    ...(json ? { "Content-Type": "application/json" } : {}),
    ...(account ? { Authorization: `Bearer ${account.token}` } : {}),
    "Accept-Language": "en", // the verdict's language: this page is in English
  };
}

async function failure(response) {
  const body = await response.json().catch(() => ({}));
  return {
    status: response.status,
    code: typeof body.error_code === "string" ? body.error_code : `HTTP_${response.status}`,
    detail: typeof body.detail === "string" ? body.detail : "",
  };
}

async function getJson(path, withAccount = false) {
  const response = await fetch(path, {
    headers: withAccount ? headers(false) : { "Accept-Language": "en" },
    cache: "no-store",
  });
  if (!response.ok) throw await failure(response);
  return response.json();
}

// ── Account ──────────────────────────────────────────────────────────────────────────────────────────────

function shortAddress(address) {
  return `${address.slice(0, 4)}…${address.slice(-4)}`;
}

function checksText(n) {
  return n === 1 ? "1 check left" : `${n} checks left`;
}

function buyPath() {
  return account?.kind === "key" ? "/checkout?for=key" : "/checkout";
}

function renderAccount() {
  $("account").hidden = !account;
  if (account) {
    $("account-status").textContent =
      account.kind === "key"
        ? `Using API key ${account.address.slice("key:".length)}.`
        : `Signed in as ${shortAddress(account.address)}.`;
    $("checks").textContent = checks === null ? "" : checksText(checks);
    $("buy-more").href = buyPath();
  }
  $("sign-in-note").textContent = expired
    ? "Your sign-in expired; your checks are still there. Sign in again to use them."
    : "Sign in with a Solana wallet, or use an API key. Your link stays here while you do.";
  $("buy-crypto").href = buyPath();
  $("buy-card-note").hidden = account?.kind === "key";
  const card = Boolean(pricing?.fiat?.available);
  $("buy-card").hidden = !card;
  $("buy-crypto").className = `button ${card ? "secondary" : "primary"}`;
}

async function refreshChecks() {
  if (!account) return;
  try {
    checks = (await getJson("/v1/credits", true)).checks_remaining;
  } catch (error) {
    if (error.status === 401) return signedOut(true);
  }
  renderAccount();
}

function signedOut(wasExpired) {
  forgetAccount();
  account = null;
  checks = null;
  expired = wasExpired;
  renderAccount();
}

function showSignIn(visible) {
  $("sign-in").hidden = !visible;
  if (visible) renderAccount();
}

function signedIn(value) {
  saveAccount(value);
  showSignIn(false);
  renderAccount();
  void refreshChecks();
  if (field.value.trim()) checkButton.focus();
  else field.focus();
}

async function pickWallet(wallet) {
  const status = $("wallet-status");
  status.textContent = `Waiting for ${wallet.name}… Check the wallet for a sign-in request.`;
  try {
    const { session } = await signIn(wallet);
    status.textContent = "";
    signedIn({ kind: "wallet", token: session.token, address: session.address, expires_at: session.expires_at });
  } catch (error) {
    status.textContent = error.message || "Sign-in failed.";
  }
}

function renderWallets(wallets) {
  $("wallets").replaceChildren(...wallets.map((wallet) => walletButton(wallet, pickWallet)));
  $("no-wallets").hidden = wallets.length > 0;
}

async function useKey() {
  const status = $("key-status");
  const key = $("key").value.trim();
  const match = KEY_RE.exec(key);
  if (!match) {
    status.textContent = "That isn't an API key (it starts with ufk_).";
    return;
  }
  status.textContent = "Checking the key…";
  try {
    const response = await fetch("/v1/credits", { headers: { Authorization: `Bearer ${key}` }, cache: "no-store" });
    if (response.status === 401) {
      status.textContent = "The server doesn't know this key (or it was replaced with its recovery code).";
      return;
    }
    if (!response.ok) throw await failure(response);
    checks = (await response.json()).checks_remaining;
    $("key").value = "";
    status.textContent = "";
    signedIn({ kind: "key", token: key, address: `key:${match[1]}` });
  } catch {
    status.textContent = "Couldn't reach Unfret. Check your connection.";
  }
}

// ── The link ─────────────────────────────────────────────────────────────────────────────────────────────

const canPaste = Boolean(navigator.clipboard?.readText);

function renderPaste() {
  pasteButton.hidden = !canPaste || field.value.trim() !== "" || running !== null;
}

function linkError(text) {
  $("link-error").textContent = text;
  $("link-error").hidden = !text;
}

// Read the clipboard only on a tap of "Paste link", never by itself.
async function paste() {
  try {
    const link = extractLink(await navigator.clipboard.readText());
    if (link) {
      field.value = link;
      linkError("");
      checkButton.focus();
    } else {
      linkError("No link found on the clipboard.");
    }
  } catch {
    linkError("The browser didn't allow reading the clipboard. Paste into the field instead.");
  }
  renderPaste();
}

// A shared link arrives as ?url=… (or #url=… through the service worker); take it and clear the address bar.
function takeSharedLink() {
  const params = new URLSearchParams(window.location.search);
  for (const [name, value] of new URLSearchParams(window.location.hash.slice(1))) params.append(name, value);
  const link = sharedLink(params);
  if (window.location.search || window.location.hash) history.replaceState(null, "", window.location.pathname);
  if (link) field.value = link;
  return link;
}

// ── Views ────────────────────────────────────────────────────────────────────────────────────────────────

function showOnly(...ids) {
  for (const id of ["progress", "result", "unreadable", "no-checks", "error", "again-row"]) $(id).hidden = !ids.includes(id);
}

function busy(on) {
  checkButton.disabled = on;
  field.readOnly = on;
  checkButton.textContent = on ? "Checking…" : "Check";
  renderPaste();
}

function buildSteps() {
  $("steps").replaceChildren(
    ...CHECKLIST.map((item) => {
      const li = el("li");
      const tick = el("span", "tick");
      tick.setAttribute("aria-hidden", "true");
      li.append(tick, el("span", undefined, item.label));
      return li;
    }),
  );
}

function updateSteps(done) {
  [...$("steps").children].forEach((li, i) => {
    if (li.classList.contains("done") === done[i]) return;
    li.classList.toggle("done", done[i]);
    li.querySelector(".tick").textContent = done[i] ? "✓" : "";
    if (done[i]) li.setAttribute("aria-label", `${CHECKLIST[i].label}: done`);
  });
}

function renderResult(result) {
  const band = bandOf(result);
  const { nodes, details } = resultNodes(result, {
    band,
    label: result.verdict?.label ?? (band ? BAND_WORD[band] : "Not assessed"),
    symbol: band ? BAND_SYMBOL[band] : "–",
  });

  if (band === null) {
    nodes.push(el("p", "small", "Unfret checks product pages and shops. Try the link to the product itself."));
  }
  nodes.push(
    el("p", "muted small source-note", "Checked from the page as Unfret's server loaded it, which can differ from what your browser shows."),
  );
  if (result.score < 0) nodes.push(el("p", "small", "No check was used."));
  else if (result.charged === false) nodes.push(el("p", "small", "Checked recently: no check was used."));

  if (details) nodes.push(details);
  $("result-body").replaceChildren(...nodes);

  const reportable = Boolean(result.report_token && account);
  $("report-area").hidden = !reportable;
  $("report-open-row").hidden = false;
  $("report").hidden = true;
  $("report-status").hidden = true;
  resetShare(result);
  const reward = pricing?.reports?.reward_checks;
  if (reward) {
    $("report-consent").textContent =
      "Sending this shares this check's link, the page as Unfret sees it and the result with Unfret for review. " +
      `Personal details are removed first; once reviewed, it is kept without any link to you. If the score was far off, you get ${reward} free checks.`;
  }
}

function showError(message, refunded) {
  $("error-message").textContent = message;
  $("error-refunded").hidden = !refunded;
  showOnly("error", "again-row");
}

function showUnreadable(code, link) {
  const text = UNREADABLE[code] ?? UNREADABLE.PAGE_UNAVAILABLE;
  $("unreadable-heading").textContent = text.heading;
  $("unreadable-body").textContent = text.body;
  let shop = "";
  try {
    shop = new URL(link).hostname.replace(/^www\./, "");
  } catch {
    // no shop name to show
  }
  $("unreadable-shop").textContent = shop ? `Shop: ${shop}` : "";
  showOnly("unreadable", "again-row");
}

// ── Checking ─────────────────────────────────────────────────────────────────────────────────────────────

async function failed(error, link) {
  if (error.status === 401) {
    signedOut(true);
    showOnly();
    showSignIn(true);
    return;
  }
  if (error.status === 402 || error.code === "INSUFFICIENT_CREDITS") {
    checks = 0;
    renderAccount();
    showOnly("no-checks");
    return;
  }
  if (error.code === "INVALID_URL" || error.code === "VALIDATION_ERROR") {
    showOnly();
    linkError("That isn't a link to a public web page.");
    field.focus();
    return;
  }
  if (error.code in UNREADABLE) {
    showUnreadable(error.code, link);
    return;
  }
  if (error.status === 429) return showError("Too many links in a short time; wait a minute and try again.", true);
  if (error.status === 503) return showError("Unfret checks aren't available right now. Please try again later.", true);
  showError("The check failed.", true);
}

async function check(link) {
  if (!account) {
    showOnly();
    showSignIn(true);
    return;
  }
  const body = { url: link };
  const abort = new AbortController();
  running = abort;
  last = null;
  busy(true);
  showOnly("progress");
  buildSteps();
  updateSteps(checklistDone([], false));
  $("progress-status").textContent = "Opening the shop's page…";
  const steps = [];
  try {
    const response = await fetch("/v1/analyze/stream", {
      method: "POST",
      headers: { ...headers(true), Accept: "text/event-stream" },
      body: JSON.stringify(body),
      signal: abort.signal,
      cache: "no-store",
    });
    if (!response.ok) return await failed(await failure(response), link);
    $("progress-status").textContent = "Checking… this usually takes 10–30 seconds.";
    const outcome = await readStream(response.body, (skill) => {
      steps.push(skill);
      updateSteps(checklistDone(steps, false));
    });
    if (outcome.kind === "result") {
      updateSteps(checklistDone(steps, true));
      last = { body, result: outcome.result };
      renderResult(outcome.result);
      showOnly("result", "again-row");
      if (typeof outcome.result.balance_usd_micros === "number") void refreshChecks();
    } else if (outcome.kind === "error") {
      showError(outcome.detail || "The check failed.", true);
    } else {
      showError("The connection was lost. Checking the same link again won't cost a second check.", false);
    }
  } catch (error) {
    if (abort.signal.aborted) showError("Cancelled. A check that hadn't finished costs nothing.", false);
    else showError("Couldn't reach Unfret. Check your connection.", false);
  } finally {
    running = null;
    busy(false);
  }
}

async function readStream(stream, onStep) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();
  const handle = (events) => {
    for (const { event, data } of events) {
      let parsed;
      try {
        parsed = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "step" && typeof parsed.skill === "string") onStep(parsed.skill);
      else if (event === "result") return { kind: "result", result: parsed };
      else if (event === "error") return { kind: "error", detail: parsed.detail };
    }
    return null;
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      const outcome = handle(done ? [...parser.feed(decoder.decode()), ...parser.end()] : parser.feed(decoder.decode(value, { stream: true })));
      if (outcome) return outcome;
      if (done) return { kind: "lost" };
    }
  } finally {
    reader.releaseLock();
  }
}

// ── "This is wrong" ──────────────────────────────────────────────────────────────────────────────────────

const REPORT_OUTCOME = {
  queued: "Thanks! We'll review this page. If we got it badly wrong, you get free checks.",
  already_reported: "Thanks! Someone reported this page first; your report counts as confirmation.",
  already_known: "Thanks! We reviewed this page recently.",
};
const REPORT_ERROR = {
  REPORT_LIMIT: "You've used this month's reports. Thank you!",
  ALREADY_REPORTED_BY_YOU: "You've already reported this page.",
  QUEUE_FULL: "Our review queue is full right now; please try again in a few days.",
  BAD_REPORT_TOKEN: "This check can't be reported any more; check the page again first.",
};

function reportStatus(text) {
  $("report-status").textContent = text;
  $("report-status").hidden = false;
}

async function sendReport(reason) {
  if (!last || !account) return;
  $("report-send").disabled = true;
  reportStatus("Sending…");
  try {
    const response = await fetch("/v1/reports", {
      method: "POST",
      headers: headers(true),
      body: JSON.stringify({
        report_token: last.result.report_token,
        reason,
        consent: true,
        request: last.body,
        explanation: last.result.explanation,
        ...(last.result.verdict ? { verdict: last.result.verdict } : {}),
      }),
    });
    if (!response.ok) {
      const error = await failure(response);
      reportStatus(REPORT_ERROR[error.code] ?? "The report didn't go through. Please try again later.");
      if (!(error.code in REPORT_ERROR)) $("report-send").disabled = false;
      return;
    }
    $("report").hidden = true;
    reportStatus(REPORT_OUTCOME[(await response.json()).status] ?? REPORT_OUTCOME.queued);
  } catch {
    reportStatus("The report didn't go through. Please try again later.");
    $("report-send").disabled = false;
  }
}

function reportReason() {
  return document.querySelector('#report input[name="reason"]:checked')?.value;
}

// ── "Share this result" ──────────────────────────────────────────────────────────────────────────────────
// Sent without the sign-in (no Authorization header): nothing on the shared page, or kept with it, says who checked.
// The server shows a preview of exactly what becomes public first; only "Share" stores it.

const SHARE_ERROR = {
  CHECKOUT_NOT_SHAREABLE: "A check that included a checkout page can't be shared.",
  SHARE_TOKEN_EXPIRED: "This check is too old to share; check the link again first.",
  BAD_SHARE_TOKEN: "This result can't be shared; check the link again first.",
  NOT_SHAREABLE: "This result can't be shared.",
};

function shareStatus(text) {
  $("share-status").textContent = text;
  $("share-status").hidden = !text;
}

function resetShare(result) {
  preview = null;
  $("share-area").hidden = !result.share_token;
  $("share-open-row").hidden = false;
  $("share-preview").hidden = true;
  $("share-done").hidden = true;
  $("share-code").textContent = "";
  shareStatus("");
}

function shareBody() {
  const { score, explanation, assessment, verdict, share_token } = last.result;
  return { share_token, url: last.body.url, result: { score, explanation, assessment, verdict } };
}

async function postShare(path) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(shareBody()),
    cache: "no-store",
  });
  if (!response.ok) throw await failure(response);
  return response.json();
}

function shareFailed(error) {
  if (error?.status === 429) return shareStatus("Too many shares from here for now; please try again later.");
  shareStatus(SHARE_ERROR[error?.code] ?? "Sharing isn't available right now. Please try again later.");
}

async function openShare() {
  if (!last?.result?.share_token) return;
  $("share-open").disabled = true;
  shareStatus("Preparing the preview…");
  try {
    preview = await postShare("/v1/shares/preview");
    const { nodes, details } = resultNodes(preview.result, {
      band: preview.band,
      label: preview.label,
      symbol: preview.symbol,
    });
    const meta = el("p", "case-meta");
    meta.append(el("span", "host", preview.host), document.createTextNode(" · "), el("span", "share-link", preview.product_url));
    $("share-preview-body").replaceChildren(
      el("p", "case-title", preview.title),
      meta,
      el("p", "muted small", `Checked by an Unfret user on ${preview.checked_on_text}; the page may have changed.`),
      ...nodes,
      ...(details ? [details] : []),
    );
    $("share-notice").replaceChildren(el("strong", undefined, preview.notice));
    $("share-open-row").hidden = true;
    $("share-preview").hidden = false;
    shareStatus("");
  } catch (error) {
    shareFailed(error);
  } finally {
    $("share-open").disabled = false;
  }
}

function onPhone() {
  return typeof navigator.share === "function" && window.matchMedia?.("(pointer: coarse)").matches;
}

async function copyLink(link) {
  try {
    await navigator.clipboard.writeText(link);
    shareStatus("Link copied.");
  } catch {
    shareStatus("Copy the link above to pass it on.");
  }
}

// On a phone, the system share sheet; elsewhere (or if it fails), the clipboard.
async function passOn(link) {
  if (onPhone()) {
    try {
      await navigator.share({ title: preview?.title ?? "Checked with Unfret", url: link });
      return;
    } catch (error) {
      if (error?.name === "AbortError") return;
    }
  }
  await copyLink(link);
}

async function confirmShare() {
  if (!preview) return;
  $("share-confirm").disabled = true;
  shareStatus("Sharing…");
  try {
    const created = await postShare("/v1/shares");
    const link = new URL(created.link, window.location.origin).href;
    $("share-link").href = link;
    $("share-link").textContent = link;
    $("share-code").textContent = created.delete_code;
    $("share-again").hidden = !onPhone();
    $("share-preview").hidden = true;
    $("share-done").hidden = false;
    shareStatus("");
    await passOn(link);
  } catch (error) {
    shareFailed(error);
  } finally {
    $("share-confirm").disabled = false;
  }
}

// ── Wiring ───────────────────────────────────────────────────────────────────────────────────────────────

form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (running) return; // one check at a time: a double tap must not pay twice
  const link = extractLink(field.value);
  if (!link) {
    linkError(field.value.trim() ? "That doesn't look like a link. Copy the whole address from the shop's page." : "Paste or type a link first.");
    field.focus();
    return;
  }
  field.value = link;
  linkError("");
  void check(link);
});
field.addEventListener("input", () => {
  linkError("");
  renderPaste();
});
pasteButton.addEventListener("click", () => void paste());
$("cancel").addEventListener("click", () => running?.abort());
$("again").addEventListener("click", () => {
  last = null;
  field.value = "";
  linkError("");
  showOnly();
  renderPaste();
  field.focus();
});
$("sign-out").addEventListener("click", () => {
  signedOut(false);
  showOnly();
});
$("key-use").addEventListener("click", () => void useKey());
$("key").addEventListener("keydown", (event) => {
  if (event.key === "Enter") void useKey();
});
$("report-open").addEventListener("click", () => {
  $("report-open-row").hidden = true;
  $("report").hidden = false;
});
$("report-cancel").addEventListener("click", () => {
  $("report").hidden = true;
  $("report-open-row").hidden = false;
});
$("report").addEventListener("change", () => {
  $("report-send").disabled = reportReason() === undefined;
});
$("share-open").addEventListener("click", () => void openShare());
$("share-cancel").addEventListener("click", () => {
  $("share-preview").hidden = true;
  $("share-open-row").hidden = false;
});
$("share-confirm").addEventListener("click", () => void confirmShare());
$("share-again").addEventListener("click", () => void passOn($("share-link").href));
$("share-copy").addEventListener("click", () => void copyLink($("share-link").href));
$("report").addEventListener("submit", (event) => {
  event.preventDefault();
  const reason = reportReason();
  if (reason) void sendReport(reason);
});

// A phone's system browser has no wallet: these reopen /check inside the wallet app's own browser.
const here = `${window.location.origin}/check`;
$("open-phantom").href =
  `https://phantom.app/ul/browse/${encodeURIComponent(here)}?ref=${encodeURIComponent(window.location.origin)}`;
$("open-solflare").href =
  `https://solflare.com/ul/v1/browse/${encodeURIComponent(here)}?ref=${encodeURIComponent(window.location.origin)}`;

account = loadAccount();
const shared = takeSharedLink();
renderAccount();
renderPaste();
showSignIn(!account);
if (account) void refreshChecks();
if (shared && account) checkButton.focus();
renderWallets([]);
discoverWallets(renderWallets);
getJson("/v1/pricing")
  .then((value) => {
    pricing = value;
    renderAccount();
  })
  .catch(() => {});
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/check-sw.js", { scope: "/check" }).catch(() => {});
