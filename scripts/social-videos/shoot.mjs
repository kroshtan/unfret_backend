// The stills each video is cut from, at 3x (a 360 px wide phone viewport makes 1080 px):
//   out/<video>/page.png    the page as a phone shows it, from the top down to video.page.height, brand bits blurred,
//                           plus page.json: where the callouts go (CSS pixels of the page).
//   out/<video>/popup.png   the store build's popup showing the real result from capture.mjs, plus popup.json: where
//                           the matching finding is.
//
// Usage: node shoot.mjs <video>
import { cpSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright";
import { EXTENSION, OUT, VIDEOS } from "./videos.mjs";

export const PHONE = { width: 360, height: 640, scale: 3 };
const MOBILE_UA =
  "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36";
const API = "https://unfret.net";
const DEV = "https://dev.unfret.net";
// The tab the popup is opened on (the popup's own address line isn't in the shot).
const TAB = "https://shop.example/listing";

const video = VIDEOS.find((v) => v.name === process.argv[2]);
if (!video) throw new Error(`usage: node shoot.mjs <video>; one of ${VIDEOS.map((v) => v.name).join(", ")}`);
for (const v of [video]) if (!v.url) throw new Error(`${v.name}: no page address; add it to pages.json (videos.mjs)`);
const dir = join(OUT, video.name);
mkdirSync(dir, { recursive: true });
const captured = JSON.parse(readFileSync(join(OUT, "results", `${video.name}.json`), "utf8"));

await shootPage();
await shootPopup();
console.log(`stills in ${dir}`);

/** The page on a phone: dismissed banners, blurred brand bits, the boxes the callouts point at. */
async function shootPage() {
  const browser = await chromium.launch({ channel: "chromium" });
  try {
    const context = await browser.newContext({
      viewport: { width: PHONE.width, height: PHONE.height },
      deviceScaleFactor: PHONE.scale,
      isMobile: true,
      hasTouch: true,
      locale: video.page.locale ?? "en-US",
      userAgent: MOBILE_UA,
      // The styles that hide and blur go in as a style tag, which a strict Content Security Policy would refuse.
      bypassCSP: true,
    });
    const page = await context.newPage();
    await page.goto(video.url, { waitUntil: "load", timeout: 60_000 });
    await page.waitForTimeout(2000);
    for (const selector of video.page.dismiss ?? []) {
      const button = page.locator(selector).first();
      if (await button.count()) await button.click({ timeout: 5000 }).catch(() => {});
    }
    await page.waitForTimeout(1500);
    const boxes = await page.evaluate(prepare, video.page);
    await page.waitForTimeout(500);
    await page.screenshot({
      path: join(dir, "page.png"),
      fullPage: true,
      clip: { x: 0, y: 0, width: PHONE.width, height: video.page.height },
    });
    writeFileSync(join(dir, "page.json"), `${JSON.stringify(boxes, null, 2)}\n`);
  } finally {
    await browser.close();
  }
}

/**
 * In the page: hide and blur what the video config says, and measure the callouts' targets.
 *
 * @param {{hide?: string[], blur?: string[], blurText?: string[], blurBoxes?: object[], marks: object}} spec
 */
function prepare(spec) {
  const style = document.createElement("style");
  style.textContent = [
    ...(spec.hide ?? []).map((s) => `${s} { display: none !important; }`),
    ...(spec.blur ?? []).map((s) => `${s} { filter: blur(7px) !important; }`),
    ".unfret-blur { filter: blur(6px); }",
  ].join("\n");
  document.head.append(style);
  // Text to blur wherever it stands (a brand name inside a title): each match in its own blurred span.
  const wrapText = (needle) => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const nodes = [];
    for (let n = walker.nextNode(); n; n = walker.nextNode()) if (n.textContent.includes(needle)) nodes.push(n);
    for (const node of nodes) {
      const parts = node.textContent.split(needle);
      const frag = document.createDocumentFragment();
      parts.forEach((part, i) => {
        if (i) {
          const span = document.createElement("span");
          span.className = "unfret-blur";
          span.textContent = needle;
          frag.append(span);
        }
        frag.append(part);
      });
      node.replaceWith(frag);
    }
  };
  for (const needle of spec.blurText ?? []) wrapText(needle);
  // A blurred patch over part of an element (a brand's mark on a product photo), in fractions of its box.
  for (const { selector, left, top, width, height } of spec.blurBoxes ?? []) {
    const el = document.querySelector(selector);
    if (!el) continue;
    const b = el.getBoundingClientRect();
    const patch = document.createElement("div");
    Object.assign(patch.style, {
      position: "absolute",
      left: `${b.left + scrollX + b.width * left}px`,
      top: `${b.top + scrollY + b.height * top}px`,
      width: `${b.width * width}px`,
      height: `${b.height * height}px`,
      backdropFilter: "blur(9px)",
      webkitBackdropFilter: "blur(9px)",
      zIndex: 2147483647,
      borderRadius: "6px",
      pointerEvents: "none",
    });
    document.body.append(patch);
  }
  // The box of a piece of text (the words a callout marks), in page CSS pixels.
  const textBox = (selector, text) => {
    const root = document.querySelector(selector) ?? document.body;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const at = n.textContent.indexOf(text);
      if (at < 0) continue;
      const range = document.createRange();
      range.setStart(n, at);
      range.setEnd(n, at + text.length);
      const rects = [...range.getClientRects()];
      const r = range.getBoundingClientRect();
      return {
        x: r.left + scrollX,
        y: r.top + scrollY,
        width: r.width,
        height: r.height,
        lines: rects.map((q) => ({ x: q.left + scrollX, y: q.top + scrollY, width: q.width, height: q.height })),
      };
    }
    return null;
  };
  // A mark's look (for a live overlay in its place, e.g. a ticking clock): the text's font and colour, and the
  // background behind it.
  const look = (selector) => {
    const el = document.querySelector(selector);
    if (!el) return null;
    const cs = getComputedStyle(el);
    let back = el, bg = "rgba(0, 0, 0, 0)";
    while (back && (bg === "rgba(0, 0, 0, 0)" || bg === "transparent")) {
      bg = getComputedStyle(back).backgroundColor;
      back = back.parentElement;
    }
    return { fontFamily: cs.fontFamily, fontSize: cs.fontSize, fontWeight: cs.fontWeight, color: cs.color,
      letterSpacing: cs.letterSpacing, fontVariantNumeric: cs.fontVariantNumeric, background: bg };
  };
  // A mark's box: a piece of text (the words a callout marks), or a whole element (no text given).
  const elementBox = (selector) => {
    const el = document.querySelector(selector);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.left + scrollX, y: r.top + scrollY, width: r.width, height: r.height, lines: [] };
  };
  const marks = {};
  for (const [name, { selector, text, style }] of Object.entries(spec.marks)) {
    marks[name] = text ? textBox(selector, text) : elementBox(selector);
    if (marks[name] && style) marks[name].style = look(selector);
  }
  return marks;
}

/** The store build's popup with the captured result, as screenshots.mjs in the extension shows it. */
async function shootPopup() {
  const extension = mkdtempSync(join(tmpdir(), "unfret-video-popup-ext-"));
  cpSync(join(EXTENSION, "dist", "chrome"), extension, { recursive: true });
  const manifestPath = join(extension, "manifest.json");
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  manifest.host_permissions = ["https://shop.example/*"];
  writeFileSync(manifestPath, JSON.stringify(manifest));
  const userData = mkdtempSync(join(tmpdir(), "unfret-video-popup-"));
  const context = await chromium.launchPersistentContext(userData, {
    channel: "chromium",
    headless: true,
    locale: "en-US",
    deviceScaleFactor: PHONE.scale,
    args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`, "--lang=en-US"],
  });
  try {
    // The account is answered here (no real pass is used to show a result); the photos come from dev.
    await context.route(`${API}/v1/credits`, (route) =>
      route.fulfill({
        json: {
          address: "key:k7m2q9xw4t",
          balance_lamports: 0,
          checks_remaining: 4,
          free_checks_remaining: 4,
          invite_eligible: false,
        },
      }),
    );
    await context.route(`${API}/v1/pricing**`, (route) => route.fulfill({ json: { network: "solana-mainnet" } }));
    await context.route(`${API}/v1/images/**`, async (route) => {
      const response = await fetch(route.request().url().replace(API, DEV));
      await route.fulfill({
        status: response.status,
        contentType: response.headers.get("content-type") ?? "image/jpeg",
        body: Buffer.from(await response.arrayBuffer()),
      });
    });
    await context.route("https://shop.example/**", (route) =>
      route.fulfill({ contentType: "text/html", body: "<!doctype html><title>Listing</title>" }),
    );
    const worker = context.serviceWorkers()[0] ?? (await context.waitForEvent("serviceworker"));
    const id = new URL(worker.url()).host;
    await worker.evaluate(async () =>
      chrome.storage.local.set({
        session: {
          token: `ufk_${"x".repeat(40)}`,
          address: "key:k7m2q9xw4t",
          expiresAt: "9999-12-31T00:00:00.000Z",
          kind: "key",
        },
        consent: { version: 2 }, // CONSENT_VERSION (src/shared/settings.ts)
      }),
    );
    const tab = await context.newPage();
    await tab.goto(TAB);
    const tabId = await worker.evaluate(async () => Math.max(...(await chrome.tabs.query({})).map((t) => t.id ?? -1)));
    await worker.evaluate(
      async ({ tabId, captured }) =>
        chrome.storage.session.set({
          [`tab:${String(tabId)}`]: {
            status: "done",
            findings: [],
            steps: [],
            body: { url: captured.url },
            result: { share_token: "t.video", ...captured.result },
          },
        }),
      { tabId, captured },
    );
    const popup = await context.newPage();
    await popup.setViewportSize({ width: PHONE.width, height: 1400 });
    await popup.goto(`chrome-extension://${id}/popup/popup.html?tab=${String(tabId)}`);
    await popup.waitForSelector("#result:not([hidden]) .verdict");
    await popup.waitForLoadState("networkidle");
    await popup.addStyleTag({
      content: `html { scrollbar-width: none; } #check, #account, #objection-row { display: none !important; }
        .unfret-blur { filter: blur(5px); }`,
    });
    const spec = video.popup;
    // The details (the signals behind the verdict), opened as a user would, when the finding is among them.
    if (spec.expand) {
      await popup.click("#result-body details > summary");
      await popup.waitForTimeout(300);
    }
    const boxes = await popup.evaluate(
      ({ blurText, finding }) => {
        for (const needle of blurText ?? []) {
          const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
          const nodes = [];
          for (let n = walker.nextNode(); n; n = walker.nextNode()) if (n.textContent.includes(needle)) nodes.push(n);
          for (const node of nodes) {
            const parts = node.textContent.split(needle);
            const frag = document.createDocumentFragment();
            parts.forEach((part, i) => {
              if (i) {
                const span = document.createElement("span");
                span.className = "unfret-blur";
                span.textContent = needle;
                frag.append(span);
              }
              frag.append(part);
            });
            node.replaceWith(frag);
          }
        }
        // The finding the video points at: the smallest block that holds its text.
        const all = [...document.querySelectorAll("#result-body li, #result-body div, #result-body p, #result-body section")];
        const hits = all.filter((e) => e.textContent.includes(finding));
        const el = hits.sort((a, b) => a.textContent.length - b.textContent.length)[0];
        const b = el?.getBoundingClientRect();
        const bottom = Math.max(...[...document.body.children].map((e) => e.getBoundingClientRect().bottom));
        return {
          finding: b ? { x: b.left, y: b.top, width: b.width, height: b.height } : null,
          height: Math.ceil(bottom + 12),
        };
      },
      { blurText: spec.blurText, finding: spec.finding },
    );
    if (!boxes.finding) throw new Error(`the popup doesn't show "${spec.finding}"`);
    await popup.setViewportSize({ width: PHONE.width, height: Math.min(boxes.height, 2400) });
    await popup.waitForTimeout(300);
    await popup.screenshot({ path: join(dir, "popup.png") });
    writeFileSync(join(dir, "popup.json"), `${JSON.stringify(boxes, null, 2)}\n`);
  } finally {
    await context.close();
    rmSync(userData, { recursive: true, force: true });
    rmSync(extension, { recursive: true, force: true });
  }
}
