// Real results for the social videos: Unfret Dev (the extension's dist/chrome-dev, API on https://dev.unfret.net,
// never production) runs a deep check on each video's page the way a user would, and the result is written to
// out/results/<video>.json, which render.mjs then shows in the store build's popup.
//
// The pass comes as on a real install: the welcome page on dev.unfret.net makes a free one (a network gets two a
// day). Or $UNFRET_DEV_PASS: a pass on dev with checks. Never an admin token.
//
// Needs the sibling extension checkout, built (`make build` in ../unfret_extension). Usage:
//   node capture.mjs [video ...]      (default: every video below)
//   node capture.mjs name=https://…   (a probe: any page, before it has a video; the result goes to out/results too)
import { cpSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright";
import { EXTENSION, OUT, VIDEOS } from "./videos.mjs";

const DEV = "https://dev.unfret.net";
const args = process.argv.slice(2);
const probes = args.filter((a) => a.includes("=")).map((a) => {
  const at = a.indexOf("=");
  return { name: a.slice(0, at), url: a.slice(at + 1), expect: "" };
});
const wanted = args.filter((a) => !a.includes("="));
const videos = [...(wanted.length || probes.length ? VIDEOS.filter((v) => wanted.includes(v.name)) : VIDEOS), ...probes];
if (videos.length === 0) throw new Error(`no such video; one of ${VIDEOS.map((v) => v.name).join(", ")}`);
for (const v of videos) if (!v.url) throw new Error(`${v.name}: no page address; add it to pages.json (videos.mjs)`);

// A copy of Unfret Dev that may read the pages without a toolbar click (what activeTab grants on a click).
const extension = mkdtempSync(join(tmpdir(), "unfret-video-ext-"));
cpSync(join(EXTENSION, "dist", "chrome-dev"), extension, { recursive: true });
const manifestPath = join(extension, "manifest.json");
const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
manifest.host_permissions = [...new Set(videos.map((v) => `${new URL(v.url).origin}/*`))];
writeFileSync(manifestPath, JSON.stringify(manifest));

const userData = mkdtempSync(join(tmpdir(), "unfret-video-profile-"));
const context = await chromium.launchPersistentContext(userData, {
  channel: "chromium",
  headless: true,
  locale: "en-US",
  viewport: { width: 1280, height: 900 },
  args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`, "--lang=en-US"],
});
try {
  const worker = context.serviceWorkers()[0] ?? (await context.waitForEvent("serviceworker"));
  const id = new URL(worker.url()).host;
  await worker.evaluate(async () => chrome.storage.local.set({ consent: { version: 2 } })); // CONSENT_VERSION
  const pass = process.env.UNFRET_DEV_PASS?.trim();
  if (pass) {
    await worker.evaluate(async (key) => {
      const res = await fetch("https://dev.unfret.net/v1/credits", { headers: { Authorization: `Bearer ${key}` } });
      if (!res.ok) throw new Error(`the pass in UNFRET_DEV_PASS was refused (${String(res.status)})`);
      const { address } = await res.json();
      await chrome.storage.local.set({
        session: { token: key, address, expiresAt: "9999-12-31T00:00:00.000Z", kind: "key" },
      });
    }, pass);
  } else {
    // The welcome page opened on install makes the free pass.
    let session;
    for (let i = 0; i < 60 && !session; i++) {
      session = await worker.evaluate(async () => (await chrome.storage.local.get("session")).session);
      if (!session) await new Promise((r) => setTimeout(r, 1000));
    }
    if (!session) throw new Error("no free pass from the welcome page (two a day per network): set UNFRET_DEV_PASS");
  }
  mkdirSync(join(OUT, "results"), { recursive: true });

  for (const video of videos) {
    const tab = await context.newPage();
    await tab.goto(video.url, { waitUntil: "load", timeout: 60_000 }).catch(() => {});
    await tab.waitForTimeout(3000);
    const tabId = await worker.evaluate(async () => Math.max(...(await chrome.tabs.query({})).map((t) => t.id ?? -1)));
    // A click on "Check this page", as a user would.
    const popup = await context.newPage();
    await popup.goto(`chrome-extension://${id}/popup/popup.html?tab=${String(tabId)}`);
    // A page Unfret can't check (gone, or not a shop page) leaves the button off: say so and go on to the next.
    const ready = await popup.waitForSelector("#analyse:not([disabled])", { timeout: 30_000 }).catch(() => null);
    if (!ready) {
      console.error(`${video.name}: the popup offers no check for this page`);
      process.exitCode = 1;
      await popup.close();
      await tab.close();
      continue;
    }
    await popup.click("#analyse");
    const state = await waitForResult(worker, tabId);
    await popup.close();
    await tab.close();
    if (state?.status !== "done" || !state.result) {
      console.error(`${video.name}: no result (${JSON.stringify(state?.error ?? state?.status ?? null)})`);
      process.exitCode = 1;
      continue;
    }
    if (state.result.tier === "quick") {
      console.error(`${video.name}: no checks left for a deep check; kept the earlier result`);
      process.exitCode = 1;
      continue;
    }
    // Only what the popup shows: no tokens, no balance.
    const dropped = new Set(["share_token", "report_token", "balance_usd_micros", "balance_lamports"]);
    const result = Object.fromEntries(Object.entries(state.result).filter(([key]) => !dropped.has(key)));
    const file = join(OUT, "results", `${video.name}.json`);
    const kept = { url: video.url, checked: new Date().toISOString().slice(0, 10), result };
    // As production shows it: the dev deployment's host becomes unfret.net.
    writeFileSync(file, `${JSON.stringify(kept, null, 2).replaceAll("dev.unfret.net", "unfret.net")}\n`);
    const found = JSON.stringify(result).includes(video.expect);
    console.log(`${video.name}: ${String(result.verdict?.label ?? result.score)} → ${file}`);
    if (!found) {
      console.error(`${video.name}: the result doesn't say "${video.expect}", the finding the video shows`);
      process.exitCode = 1;
    }
  }
} finally {
  await context.close();
  rmSync(userData, { recursive: true, force: true });
  rmSync(extension, { recursive: true, force: true });
}

/**
 * The tab's state once its check is done or failed (the background keeps it in session storage).
 *
 * @param {import("playwright").Worker} worker The extension's service worker.
 * @param {number} tabId The checked tab.
 */
async function waitForResult(worker, tabId) {
  for (let i = 0; i < 300; i++) {
    const state = await worker.evaluate(async (key) => (await chrome.storage.session.get(key))[key], `tab:${String(tabId)}`);
    if (state && (state.status === "done" || state.status === "error")) return state;
    await new Promise((r) => setTimeout(r, 1000));
  }
  return undefined;
}
