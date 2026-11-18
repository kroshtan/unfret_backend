// /check: check a product link from any browser, phones first. The link goes to POST /v1/analyze/stream with only
// a URL; the server fetches the page itself and streams progress, then the result. A page it can't check that way
// (the shop blocks automatic visitors, a bot check, a page that needs a browser) comes back as a free 422, shown as
// such and never as a verdict.
//
// On a phone, this page in the browser is Unfret (it doesn't install as an app). Its checks come from an Unfret pass
// (an API key) or, for those who bought with a crypto wallet, a wallet's sign-in from /my-checks. A first visit gets
// a free pass at the first tap of Check (new-pass.js: a few free deep checks, asked for once per browser); after that,
// or without a pass, quick checks (rules only) are free and the page offers to get checks (/checkout) or to paste a
// pass; a pass also arrives by QR code from /my-checks in another browser (/check#pass=ufk_…, read here and cleared
// from the address bar at once). A deep check that only free checks could pay for, once they're used (402) or today's
// are all used (429 FREE_CHECKS_PAUSED), is answered with a quick check instead, saying so.
//
// A pass the server no longer knows (401 INVALID_API_KEY: retired at Unfret's free start, or replaced elsewhere) is
// dropped and this browser starts over with the free pass, the page saying so once (passRetired).
//
// What this page keeps, in localStorage: the sign-in (the pass, or a wallet's session token) until it's removed on
// /my-checks, and that this browser got its free pass (FREE_GIVEN, so it gets one only once). No history: the link lives in the text field and in memory for the one check (and goes into a shared
// result only when the shopper shares it, after a preview). A shared link (/check?url=… or /check#url=…) is read here and removed
// from the address bar at once.
import {
  BAND_SYMBOL,
  BAND_WORD,
  CHECKLISTS,
  SseParser,
  bandOf,
  checklistDone,
  checklistFor,
  extractLink,
  passEnded,
  passEndedHint,
  sharedLink,
  shopOf,
  unreadableKind,
} from "./check-core.js";
import { deepHintNode, deepHintOf, el, resultNodes } from "./check-render.js";
import { PASS_RE, passIdOf, showPassId } from "./pass.js";
import { makePass } from "./new-pass.js";
import { takeHandedLink } from "./start-check.js";
import { INVITE_RE, NOT_A_CODE, api, redeemInvite } from "./wallet.js";

const STORE_KEY = "unfret-account";
const FREE_GIVEN = "unfret-free-pass"; // this browser asked for its free pass (once per browser)
const NEW_KEY = "unfret-new-key"; // a pass made in this browser, with its backup code (as /checkout keeps one)
const FREE_PER_PASS = 5; // until /v1/pricing says
const UNREADABLE = {
  PAGE_BLOCKED: {
    heading: "This site turns automatic visits away",
    body: "It refused Unfret, or showed it a robot check instead of the page. Unfret won't guess from half a page, so there is no verdict.",
  },
  PAGE_TOO_THIN: {
    heading: "This page only shows itself in a browser",
    body: "Opened by Unfret, it was almost empty: too little to judge. If this was a search or home page, try the link to the product, the ad or the company itself.",
  },
  PAGE_UNAVAILABLE: {
    heading: "Couldn't open this page",
    body: "The address may be wrong, the page gone or the site down. Check the link and try again.",
  },
};
// A product, an ad or a profile that came back empty: the shop builds its pages in the browser, so a hint to try the
// product's own link would send the shopper round in circles.
const BROWSER_ONLY_ITEM = {
  heading: "This shop only shows its pages in a browser",
  body: "Opened by Unfret's server, the page was almost empty: the shop builds it in your browser. Pages from this shop only work with the Unfret browser extension or the Android app.",
};
// The big shops known to turn the server away (listed on the page, from the server: routes/pages.py).
const BROWSER_ONLY_SHOPS = [...document.querySelectorAll("#browser-only [data-hosts]")].map((item) => ({
  name: item.textContent.trim(),
  hosts: item.dataset.hosts.split(" ").filter(Boolean),
}));

const $ = (id) => document.getElementById(id);
const form = $("form");
const field = $("link");
const pasteButton = $("paste");
const checkButton = $("check");

let account = null; // { kind: "wallet" | "key", token, address, expires_at }
let expired = false;
let ended = false; // the pass that was here was retired (the server no longer knows it): a new free start
let checks = null;
let freeLeft = 0; // of `checks`, the free ones
let freePaused = false; // today's free deep checks are all used
let apiFree = 0; // a developer pass's free checks: for the API and the MCP server, not this page
let inviteEligible = false;
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
  ended = false;
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

function freeGiven() {
  try {
    return localStorage.getItem(FREE_GIVEN) !== null;
  } catch {
    return false; // no storage: each visit may ask, and the server's own limits decide
  }
}

function markFreeGiven() {
  try {
    localStorage.setItem(FREE_GIVEN, "1");
  } catch {
    // not remembered
  }
}

// The pass made in this browser, if it is the one that ended: its backup code is no use now, and a new one may be kept.
function forgetNewPass(keyId) {
  try {
    if (JSON.parse(localStorage.getItem(NEW_KEY) ?? "null")?.key_id === keyId) localStorage.removeItem(NEW_KEY);
  } catch {
    // nothing kept
  }
}

function keepNewPass(issued) {
  try {
    if (localStorage.getItem(NEW_KEY) !== null) return;
    const kept = { key: issued.key, recovery_code: issued.recovery_code, key_id: issued.key_id, funded: false };
    localStorage.setItem(NEW_KEY, JSON.stringify(kept));
  } catch {
    // not remembered
  }
}

// ── Network ──────────────────────────────────────────────────────────────────────────────────────────────

function headers(json) {
  return {
    ...(json ? { "Content-Type": "application/json" } : {}),
    ...(account ? { Authorization: `Bearer ${account.token}` } : {}),
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
    headers: withAccount ? headers(false) : {},
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
  if (freeLeft > 0 && freeLeft === n) return n === 1 ? "1 free check left" : `${n} free checks left`;
  return n === 1 ? "1 check left" : `${n} checks left`;
}

function freePerPass() {
  return pricing?.free?.checks_per_pass ?? FREE_PER_PASS;
}

// No pass yet and no free pass asked for in this browser: the first Check gets one.
function freeAvailable() {
  return !account && !freeGiven() && freePerPass() > 0;
}

// Signed in with a key: its top-up, the key's ID filled in (the ID only: never the key itself in an address).
function buyPath() {
  if (account?.kind !== "key") return "/checkout";
  const id = account.address.slice("key:".length);
  return /^[a-z2-7]{10}$/.test(id) ? `/checkout?mode=topup&key_id=${id}` : "/checkout?mode=topup";
}

function renderAccount() {
  $("account").hidden = !account;
  if (account) {
    $("account-status").textContent =
      account.kind === "key"
        ? `on pass ${showPassId(account.address.slice("key:".length))}`
        : `on wallet ${shortAddress(account.address)}`;
    $("checks").textContent =
      checks === null
        ? ""
        : checksText(checks) + (apiFree > 0 ? ` (a developer pass: its ${apiFree} free checks are for the API)` : "");
    $("buy-more").href = buyPath();
  }
  // The gift-code field: shown for any pass or wallet (a gift code works on any account but its buyer's).
  $("invite").hidden = !(account && inviteEligible);
  $("sign-in-note").textContent = expired
    ? "Your wallet's sign-in expired; your checks are still there. Sign in again on My checks."
    : "Quick checks are free. Deep checks come in packs, with no subscription and no account.";
  // Out of checks: add checks to this pass (or to this wallet, in crypto).
  $("buy-card").href = account?.kind === "wallet" ? "/checkout?mode=crypto" : buyPath();
  $("buy-card").textContent = account?.kind === "key" ? "Add checks to my pass" : "Buy checks";
  $("no-checks-pass").hidden = Boolean(account);
  // A first visit: the first check is on the house, no account and nothing to buy; a pass you have is one step away.
  const fresh = freeAvailable();
  $("have-pass-row").hidden = !fresh;
  if (!running) {
    $("hint").textContent = ended
      ? passEndedHint(fresh ? freePerPass() : 0)
      : fresh
      ? `Your first ${freePerPass()} checks are free: no account, nothing to buy. Nothing is sent until you tap Check.`
      : account && checks === 0
        ? "Quick checks are free. Nothing is sent until you tap Check."
        : account && freePaused && freeLeft === checks
          ? "Free deep checks are back tomorrow; quick checks still work. Nothing is sent until you tap Check."
          : !account
            ? "Quick checks are free. Nothing is sent until you tap Check."
            : "One check per link. Nothing is sent until you tap Check.";
  }
}

async function refreshChecks() {
  if (!account) return;
  try {
    const credits = await getJson("/v1/credits", true);
    checks = credits.checks_remaining;
    freeLeft = credits.free_checks_remaining ?? 0;
    freePaused = credits.free_checks_paused === true;
    apiFree = credits.api_free_checks_remaining ?? 0;
    inviteEligible = credits.invite_eligible === true;
  } catch (error) {
    if (passEnded(error, account)) return passRetired();
    if (error.status === 401) return signedOut(true);
  }
  renderAccount();
}

// A pass the server no longer knows (check-core's passEnded): dropped without fuss, with a pass made here and kept
// with its backup code; this browser starts over with the free pass, and the page says so once.
function passRetired() {
  forgetNewPass(account?.address.slice("key:".length));
  signedOut(false);
  ended = true;
  renderAccount();
}

function signedOut(wasExpired) {
  forgetAccount();
  account = null;
  checks = null;
  freeLeft = 0;
  apiFree = 0;
  expired = wasExpired;
  renderAccount();
}

function showSignIn(visible) {
  $("sign-in").hidden = !visible;
  if (visible) renderAccount();
}

// ── The free pass (a first visit) ─────────────────────────────────────────────────────────────────────────────

// Asked for once per browser: whatever the server gives (5 free checks, or none once this network had its share
// today), this browser has had its turn. On a failure (offline, too many new passes from here) nothing is kept.
async function getFreePass() {
  $("progress-status").textContent = "Getting your free checks… this takes a few seconds.";
  try {
    const issued = await makePass(api, { free: true });
    markFreeGiven();
    keepNewPass(issued);
    freeLeft = issued.free_checks ?? 0;
    checks = freeLeft;
    saveAccount({ kind: "key", token: issued.key, address: issued.account });
    renderAccount();
    return true;
  } catch {
    return false;
  }
}

function signedIn(value) {
  saveAccount(value);
  showSignIn(false);
  renderAccount();
  void refreshChecks();
  if (field.value.trim()) checkButton.focus();
  else field.focus();
}

// A pass, checked with the server before it is kept: { ok: true, checks } or { ok: false, reason }.
async function tryPass(pass) {
  try {
    const response = await fetch("/v1/credits", { headers: { Authorization: `Bearer ${pass}` }, cache: "no-store" });
    if (response.status === 401) {
      return { ok: false, reason: "This pass no longer works: it was replaced, with its backup code or after a payment in another browser." };
    }
    if (!response.ok) return { ok: false, reason: "Couldn't check the pass right now. Please try again." };
    const credits = await response.json();
    return { ok: true, checks: credits.checks_remaining, inviteEligible: credits.invite_eligible === true };
  } catch {
    return { ok: false, reason: "Couldn't reach Unfret. Check your connection." };
  }
}

async function useKey() {
  const status = $("key-status");
  const pass = $("key").value.trim();
  const id = passIdOf(pass);
  if (!id) {
    status.textContent = "That doesn't look like an Unfret pass. Copy it again in full.";
    return;
  }
  status.textContent = "Checking your pass…";
  const tried = await tryPass(pass);
  if (!tried.ok) {
    status.textContent = tried.reason;
    return;
  }
  checks = tried.checks;
  inviteEligible = tried.inviteEligible;
  $("key").value = "";
  status.textContent = "";
  signedIn({ kind: "key", token: pass, address: `key:${id}` });
}

// A pass from a QR code on /my-checks (/check#pass=ufk_…): kept like a pasted one, once the visitor says so. Any site
// can open such a link, so it always asks; and if this device already uses another pass or a wallet, it says so: the
// one here may be the only copy of a pass with checks on it.
async function receivePass(pass) {
  const id = passIdOf(pass);
  if (!id || account?.token === pass) return;
  const take = async () => {
    const tried = await tryPass(pass);
    $("pass-offer").hidden = true;
    if (!tried.ok) return showPassAdded(tried.reason);
    checks = tried.checks;
    inviteEligible = tried.inviteEligible;
    signedIn({ kind: "key", token: pass, address: `key:${id}` });
    showPassAdded(`Your pass ${showPassId(id)} is on this device now.`);
  };
  $("pass-offer-id").textContent = showPassId(id);
  $("pass-offer-other").hidden = !account;
  $("pass-offer").hidden = false;
  $("pass-offer-use").onclick = () => void take();
  $("pass-offer-keep").onclick = () => {
    $("pass-offer").hidden = true;
  };
}

function showPassAdded(text) {
  $("pass-added-text").textContent = text;
  $("pass-added").hidden = false;
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

// A shared link arrives as ?url=… or #url=…, a pass as #pass=…; take them and clear the address bar.
function takeSharedLink() {
  const params = new URLSearchParams(window.location.search);
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const pass = fragment.get("pass");
  fragment.delete("pass");
  for (const [name, value] of fragment) params.append(name, value);
  const link = sharedLink(params);
  if (window.location.search || window.location.hash) history.replaceState(null, "", window.location.pathname);
  if (link) field.value = link;
  return { link, pass: pass && PASS_RE.test(pass) ? pass : null };
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

let stepsKind = "shop";

function buildSteps(kind = "shop") {
  stepsKind = kind;
  $("steps").replaceChildren(
    ...CHECKLISTS[kind].map((item) => {
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
    if (done[i]) li.setAttribute("aria-label", `${CHECKLISTS[stepsKind][i].label}: done`);
  });
}

function renderResult(result, because = null) {
  const band = bandOf(result);
  const { nodes } = resultNodes(result, {
    band,
    label: result.verdict?.label ?? (band ? BAND_WORD[band] : "Not assessed"),
    symbol: band ? BAND_SYMBOL[band] : "–",
  });

  if (band === null) {
    nodes.push(
      el("p", "small", "Unfret checks products on marketplaces and web shops, second-hand ads, and the websites of tradespeople and local services. Try the link to the product, the ad or the business itself."),
    );
  }
  nodes.push(
    el("p", "muted small source-note", "Unfret opened this page itself, so it may look a little different from what you see."),
  );
  const hint = deepHintOf(result);
  if (result.tier === "quick") {
    nodes.push(el("p", "small", "Quick check: Unfret read only this page, by its rules, without looking the shop up elsewhere. Free, and no check was used."));
    // Once, under the quick result: what a deep check would add here, what it costs, one button.
    if (hint) nodes.push(deepHintNode(hint, { run: runDeep, checkoutHref: checkoutHref() }));
  } else if (result.score < 0) nodes.push(el("p", "small", "No check was used."));
  else if (result.charged === false) nodes.push(el("p", "small", "You checked this page recently: no new check was used."));
  else if (result.free_check) nodes.push(el("p", "small", "One of your free checks."));
  const reason = because ? QUICK_BECAUSE[because] : null;
  if (reason) {
    $("no-checks-heading").textContent = reason.heading;
    // With the suggestion under the result, this says only why the check was quick: the offer is made once.
    $("no-checks-body").textContent = hint ? reason.short : reason.body;
  }
  $("buy-card").hidden = Boolean(hint);

  $("result-body").replaceChildren(...nodes);

  const reportable = Boolean(result.report_token && account);
  $("report-area").hidden = !reportable;
  $("report-open-row").hidden = false;
  $("report").hidden = true;
  $("report-status").hidden = true;
  resetShare(result);
  $("report-consent").textContent = reportConsent(pricing?.reports);
}

function showError(message, refunded) {
  $("error-message").textContent = message;
  $("error-refunded").hidden = !refunded;
  showOnly("error", "again-row");
}

/** What to say about a page the server couldn't read: by the reason, the shop and whether the link is one item. */
function unreadableText(code, link) {
  const kind = unreadableKind(code, link, BROWSER_ONLY_SHOPS);
  if (kind === "shop") {
    return {
      heading: UNREADABLE[code === "PAGE_TOO_THIN" ? code : "PAGE_BLOCKED"].heading,
      body: `${shopOf(link, BROWSER_ONLY_SHOPS).name} turns automatic checks away, so its pages only work with the Unfret browser extension or the Android app.`,
    };
  }
  if (kind === "item") return BROWSER_ONLY_ITEM;
  return UNREADABLE[code] ?? UNREADABLE.PAGE_UNAVAILABLE;
}

function showUnreadable(code, link) {
  const text = unreadableText(code, link);
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
  if (passEnded(error, account)) {
    passRetired();
    showOnly();
    return;
  }
  if (error.status === 401) {
    signedOut(true);
    showOnly();
    showSignIn(true);
    return;
  }
  if (error.status === 402 || error.code === "INSUFFICIENT_CREDITS") {
    checks = 0;
    renderAccount();
    $("no-checks-heading").textContent = "You have no checks left";
    $("no-checks-body").textContent = "Add a pack and check on. Nothing was used for this link.";
    $("buy-card").hidden = false;
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

// Why a quick check ran instead of a deep one, in the words shown above its result.
const QUICK_BECAUSE = {
  used: {
    heading: "You have no checks left",
    body: "Here's a quick check instead: free, it reads only this page, by its rules. A deep check also looks the shop up elsewhere; checks come in packs, with no subscription.",
    short: "Here's a quick check instead: free, it reads only this page, by its rules.",
  },
  paused: {
    heading: "Free deep checks are back tomorrow",
    body: "Today's free deep checks are all used, so here's a quick check: free, it reads only this page, by its rules. Bought checks work as always.",
    short: "Today's free deep checks are all used, so here's a quick check: free, it reads only this page, by its rules.",
  },
  none: {
    heading: "A quick check",
    body: "Without a pass, Unfret reads only this page, by its rules. A deep check also looks the shop up elsewhere: get checks, or use your pass.",
    short: "Without a pass, Unfret reads only this page, by its rules.",
  },
};

// Where "Get checks" goes: this pass's top-up, a wallet's crypto checkout, or the checkout.
function checkoutHref() {
  return account?.kind === "wallet" ? "/checkout?mode=crypto" : buyPath();
}

// "Run a deep check" under a quick result: a normal check of the same link (charged as any deep check).
function runDeep() {
  const link = last?.body?.url;
  if (!link || running) return;
  field.value = link;
  void check(link);
}

async function check(link) {
  const abort = new AbortController();
  running = abort;
  last = null;
  busy(true);
  showOnly("progress");
  buildSteps();
  updateSteps(checklistDone([], false));
  try {
    // A first check makes a free pass first (seconds of proof of work on a phone); for a shop known to turn the
    // server away, ask the server first, so its "no" comes at once and the free pass waits for a page it can read.
    if (freeAvailable() && shopOf(link, BROWSER_ONLY_SHOPS) && (await unreadableFirst(link, abort))) return;
    if (freeAvailable()) await getFreePass();
    if (!account) return await run(link, "quick", abort, "none");
    const outcome = await run(link, "deep", abort);
    if (outcome === "used" || outcome === "paused") await run(link, "quick", abort, outcome);
  } finally {
    running = null;
    busy(false);
  }
}

// Whether the server can't read `link`, asked with a free quick check (no pass needed): true when that's on screen.
// A page it can read after all is left for the usual check (the quick check's stream is dropped unread).
async function unreadableFirst(link, abort) {
  $("progress-status").textContent = "Opening the shop's page…";
  try {
    const response = await fetch("/v1/analyze/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ url: link, tier: "quick" }),
      signal: abort.signal,
      cache: "no-store",
    });
    if (response.ok) {
      void response.body?.cancel();
      return false;
    }
    const error = await failure(response);
    if (!(error.code in UNREADABLE)) return false;
    showUnreadable(error.code, link);
    return true;
  } catch {
    if (!abort.signal.aborted) return false; // the usual check says what went wrong
    showError("Cancelled. A check that hadn't finished costs nothing.", false);
    return true;
  }
}

// One check of `link`; resolves "used" or "paused" when a deep check needs checks the account doesn't have (a quick
// check follows), else when the outcome is on screen.
async function run(link, tier, abort, because = null) {
  const body = tier === "quick" ? { url: link, tier } : { url: link };
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
    if (!response.ok) {
      const error = await failure(response);
      if (tier === "deep" && error.code === "FREE_CHECKS_PAUSED") return "paused";
      if (tier === "deep" && (error.status === 402 || error.code === "INSUFFICIENT_CREDITS")) {
        checks = 0;
        renderAccount();
        return "used";
      }
      return await failed(error, link);
    }
    $("progress-status").textContent =
      tier === "quick" ? "A quick check… a few seconds." : "Checking… this usually takes 10–30 seconds.";
    const outcome = await readStream(response.body, (skill, pageType) => {
      // The classifier names the kind of page: a shop, a second-hand ad or a company; its own checklist follows.
      if (pageType && checklistFor(pageType) !== stepsKind) buildSteps(checklistFor(pageType));
      steps.push(skill);
      updateSteps(checklistDone(steps, false, stepsKind));
    });
    if (outcome.kind === "result") {
      updateSteps(checklistDone(steps, true, stepsKind));
      last = { body, result: outcome.result };
      renderResult(outcome.result, because);
      showOnly("result", ...(because ? ["no-checks"] : []), "again-row");
      if (account) void refreshChecks();
    } else if (outcome.kind === "error") {
      showError(outcome.detail || "The check failed.", true);
    } else {
      showError("The connection was lost. Checking the same link again won't cost a second check.", false);
    }
  } catch (error) {
    if (abort.signal.aborted) showError("Cancelled. A check that hadn't finished costs nothing.", false);
    else showError("Couldn't reach Unfret. Check your connection.", false);
  }
  return null;
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
      if (event === "step" && typeof parsed.skill === "string") onStep(parsed.skill, parsed.page_type);
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

// ── "Wrong result?" ─────────────────────────────────────────────────────────────────────────────────────

/** The consent line, with the rewards the server pays (`/v1/pricing` → reports) when it says them. */
function reportConsent(reports) {
  const base =
    "Sending this shares this check's link, the page as Unfret sees it and the result with Unfret for review. " +
    "Personal details are removed first; an AI model may suggest a label, a person decides; once reviewed, it is " +
    "kept without any link to you.";
  const big = reports?.reward_checks;
  if (!big) return base;
  const small = reports.small_reward_checks;
  const checks = (n) => `${n} free check${n === 1 ? "" : "s"}`;
  const reward = small
    ? `If Unfret was clearly wrong, you get ${checks(big)}; if it was somewhat off, ${checks(small)}.`
    : `If Unfret was clearly wrong, you get ${checks(big)}.`;
  return `${base} ${reward}`;
}

const REPORT_OUTCOME = {
  queued: "Thanks! We'll review this page. If we got it wrong, you get free checks.",
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
    const { nodes } = resultNodes(preview.result, {
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
// "Have an invite?": a code someone gave the user, for this (new) pass or wallet.
$("invite-redeem").addEventListener("click", async () => {
  const status = $("invite-status");
  const code = $("invite-input").value.trim().toLowerCase();
  if (!INVITE_RE.test(code)) {
    status.textContent = NOT_A_CODE;
    return;
  }
  if (!account) return;
  $("invite-redeem").disabled = true;
  try {
    const added = await redeemInvite(code, account.token);
    $("invite-input").value = "";
    status.textContent = `✓ ${added.checks_added} checks added.`;
    checks = added.checks_remaining;
    renderAccount();
  } catch (error) {
    if (passEnded(error, account)) {
      passRetired();
      status.textContent = "";
      return;
    }
    if (error.status === 401) signedOut(true);
    status.textContent = error.message || "That code didn't work.";
  } finally {
    $("invite-redeem").disabled = false;
  }
});
// The examples are links to the made-up shops: a plain click checks one here; a click for a new tab opens the shop.
for (const example of document.querySelectorAll("[data-example]")) {
  example.addEventListener("click", (event) => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (running) return;
    field.value = new URL(example.dataset.example, window.location.origin).href;
    linkError("");
    renderPaste();
    form.requestSubmit();
  });
}
for (const id of ["have-pass", "no-checks-pass"]) {
  $(id).addEventListener("click", (event) => {
    event.preventDefault();
    showSignIn(true);
    $("choice-key").open = true;
    $("key").focus();
  });
}
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

account = loadAccount();
const shared = takeSharedLink();
renderAccount();
renderPaste();
showSignIn(!account && !freeAvailable());
if (account) void refreshChecks();
if (shared.link && account) checkButton.focus();
if (shared.pass) void receivePass(shared.pass);
// A link pasted on the home page or /nl (start-check.js) is checked at once, as if Check was tapped here. Only this
// site's own pages can hand one over (this tab's session storage), so no other site can spend a check this way.
const handed = takeHandedLink();
if (handed && !shared.link && !shared.pass) {
  field.value = handed;
  renderPaste();
  if (form.requestSubmit) form.requestSubmit();
  else form.dispatchEvent(new Event("submit", { cancelable: true }));
}
getJson("/v1/pricing")
  .then((value) => {
    pricing = value;
    renderAccount();
  })
  .catch(() => {});
// This page used to install as a web app, with a service worker. A browser that still has that worker drops it here
// (the worker's last version, /check-sw.js, removes itself too, the next time the browser looks for an update).
navigator.serviceWorker
  ?.getRegistrations()
  .then((registrations) => {
    for (const registration of registrations) registration.unregister();
  })
  .catch(() => {});
