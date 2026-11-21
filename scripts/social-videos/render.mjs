// Renders a video from its stills (shoot.mjs) and its script (videos.mjs): every frame is an HTML stage at 1080x1920
// set to time t and screenshotted, piped into ffmpeg as an H.264 MP4 (30 fps), then a GIF from the MP4. Both land in
// out/<video>/ and are copied to DELIVER (media/videos in the metarepo).
//
// ffmpeg: $FFMPEG, else `ffmpeg` on the PATH (any build with libx264; `uv run --with imageio-ffmpeg` ships one).
// Usage: node render.mjs <video> [--mascot] [--frames t1,t2,...]   (--frames: only those stills, as PNGs, for
// checking; --mascot: with the animated mascot from the script's `mascot` track, as <video>-mascot.mp4 and .gif)
import { spawn } from "node:child_process";
import { copyFileSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "playwright";
import { mascotMarkup, mascotRuntime } from "./mascot.mjs";
import { DELIVER, HERE, OUT, VIDEOS } from "./videos.mjs";

const W = 1080;
const H = 1920;
const FPS = 30;
const SCALE = 3; // stills are 3x the phone's CSS pixels
// The phone browser's own bars (status bar, address bar) above the page, in frame px.
const STATUS = 66;
const TOOLBAR = 138;
const VIEW_TOP = STATUS + TOOLBAR;
const STATIC = join(HERE, "..", "..", "src", "unfret", "static");
const FFMPEG = process.env.FFMPEG ?? "ffmpeg";

const args = process.argv.slice(2);
const video = VIDEOS.find((v) => v.name === args[0]);
if (!video) throw new Error(`usage: node render.mjs <video>; one of ${VIDEOS.map((v) => v.name).join(", ")}`);
const only = args.includes("--frames") ? args[args.indexOf("--frames") + 1].split(",").map(Number) : null;
const withMascot = args.includes("--mascot");
if (withMascot && !video.script.mascot) throw new Error(`${video.name} has no mascot track in videos.mjs`);
const mascot = withMascot ? video.script.mascot : null;
const output = video.name + (withMascot ? "-mascot" : "");
const dir = join(OUT, video.name);
const marks = JSON.parse(readFileSync(join(dir, "page.json"), "utf8"));
const popup = JSON.parse(readFileSync(join(dir, "popup.json"), "utf8"));
const url = (file) => pathToFileURL(file).href;

const stage = `<!doctype html><html><head><meta charset="utf-8"><style>
@font-face { font-family: Figtree; font-weight: 300 900; src: url("${url(join(STATIC, "fonts", "figtree-latin-wght-normal.woff2"))}") format("woff2"); }
@font-face { font-family: Figtree; font-weight: 300 900; src: url("${url(join(STATIC, "fonts", "figtree-latin-ext-wght-normal.woff2"))}") format("woff2");
  unicode-range: U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+1E00-1E9F, U+2C60-2C7F, U+A720-A7FF; }
:root { --green: #1F8A70; --deep: #0F5C4A; --ink: #12302A; --paper: #F3F8F5; --soft: #E1F1EA; --caution: #B0562A;
  --caution-soft: #FBEAE1; }
html, body { margin: 0; width: ${W}px; height: ${H}px; overflow: hidden; background: #fff; font-family: Figtree, sans-serif; }
.scene { position: absolute; inset: 0; overflow: hidden; }
/* The phone browser. */
#view { position: absolute; left: 0; top: ${VIEW_TOP}px; width: ${W}px; height: ${H - VIEW_TOP}px; overflow: hidden; background: #fff; }
#layer { position: absolute; left: 0; top: 0; transform-origin: 0 0; will-change: transform; }
#layer img { display: block; width: ${W}px; }
#layer svg { position: absolute; left: 0; top: 0; overflow: visible; }
/* A live clock over the page's own (script.clocks), and the white flash of a reload (script.reloads). */
.clock { position: absolute; box-sizing: border-box; white-space: nowrap; display: flex; align-items: center; }
#reload { position: absolute; inset: 0; background: #fff; opacity: 0; }
#chrome { position: absolute; left: 0; top: 0; width: ${W}px; height: ${VIEW_TOP}px; background: #f1f3f4;
  box-shadow: 0 2px 0 rgba(0,0,0,.06); }
#status { height: ${STATUS}px; display: flex; align-items: center; justify-content: space-between; padding: 0 44px;
  font: 600 34px Figtree; color: #202124; box-sizing: border-box; }
#bar { margin: 10px 30px 0; height: 104px; border-radius: 52px; background: #fff; display: flex; align-items: center;
  gap: 24px; padding: 0 36px; box-sizing: border-box; }
.addr { height: 30px; width: 420px; border-radius: 15px; background: #c9cdd1; filter: blur(7px); }
/* Captions. */
.cap { position: absolute; left: 50%; max-width: 940px; box-sizing: border-box; padding: .22em .55em .26em;
  border-radius: .42em; font-weight: 800; line-height: 1.1; letter-spacing: -.01em; text-align: center;
  white-space: nowrap; color: var(--ink); background: #fff; box-shadow: 0 10px 34px rgba(18,48,42,.22);
  opacity: 0; }
.cap.caution { color: #fff; background: var(--caution); }
.cap.soft { color: var(--ink); background: var(--soft); font-weight: 700; }
.cap.ink { color: #fff; background: var(--ink); }
/* The check. */
#check { background: var(--paper); transform: translateY(100%); box-shadow: 0 -20px 60px rgba(18,48,42,.25); }
#card { position: absolute; left: 66px; top: 420px; width: 948px; height: 1060px; border-radius: 40px; overflow: hidden;
  background: #fff; box-shadow: 0 24px 70px rgba(18,48,42,.22); }
#card img { position: absolute; left: 0; top: 0; width: 948px; }
#check svg { position: absolute; left: 0; top: 0; overflow: visible; }
/* The end card. */
#end { background: var(--paper); opacity: 0; display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 0; color: var(--ink); }
#end img { width: 250px; height: 250px; }
#end .name { font-size: 76px; font-weight: 800; margin-top: 26px; }
#end .line { font-size: 92px; font-weight: 800; margin-top: 70px; letter-spacing: -.015em; }
#end .web { font-size: 66px; font-weight: 700; color: var(--green); margin-top: 34px; }
#end .free { font-size: 44px; color: var(--deep); margin-top: 70px; }
</style></head><body>
<section class="scene" id="page">
  <div id="view"><div id="layer"><img src="${url(join(dir, "page.png"))}"><svg id="marks"></svg></div>
    <div id="reload"></div></div>
  <div id="chrome">
    <div id="status"><span>9:41</span><span style="display:flex;gap:16px;align-items:center">
      <svg width="34" height="26" viewBox="0 0 34 26"><path d="M2 24h5V16H2zM10 24h5V11h-5zM18 24h5V6h-5zM26 24h5V1h-5z" fill="#202124"/></svg>
      <svg width="56" height="28" viewBox="0 0 56 28"><rect x="1.5" y="1.5" width="48" height="25" rx="6" fill="none" stroke="#202124" stroke-width="3"/><rect x="6" y="6" width="32" height="16" rx="3" fill="#202124"/><rect x="51" y="9" width="4" height="10" rx="2" fill="#202124"/></svg></span></div>
    <div id="bar">
      <svg width="30" height="38" viewBox="0 0 30 38"><rect x="2" y="16" width="26" height="20" rx="4" fill="#5f6368"/><path d="M8 16V11a7 7 0 0 1 14 0v5" fill="none" stroke="#5f6368" stroke-width="4"/></svg>
      <span class="addr"></span></div>
  </div>
</section>
<section class="scene" id="check">
  <div id="card"><img src="${url(join(dir, "popup.png"))}"></div>
  <svg id="box" width="${W}" height="${H}"></svg>
</section>
<section class="scene" id="end">
  <img src="${url(join(STATIC, "icons", "icon.svg"))}">
  <div class="name">Unfret</div>
  <div class="line">Check before you pay.</div>
  <div class="web">unfret.net</div>
  <div class="free">Quick checks are free.</div>
</section>
<div id="caps"></div>
${mascot ? mascotMarkup(mascot, W, H) : ""}
<script>
const S = ${JSON.stringify({ script: video.script, marks, popup, W, H, SCALE, VIEW_TOP, mascot })};
const { script } = S;
const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
const ease = (x) => (x < .5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);
const progress = (t, from, length) => ease(clamp((t - from) / length));

// A seeded wobble, so the "hand-drawn" strokes are the same in every frame and every run.
let seed = 7;
const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647) - 0.5;

/** A loose marker loop round a box (frame px of the still): a bit more than one turn, never quite round. */
function circlePath(b) {
  const cx = b.x + b.width / 2, cy = b.y + b.height / 2;
  const rx = b.width / 2 + 26, ry = b.height / 2 + 20;
  const points = [];
  const start = -2.4, turn = Math.PI * 2 * 1.12;
  const wob = [rnd(), rnd(), rnd()];
  for (let i = 0; i <= 64; i++) {
    const a = start + (turn * i) / 64;
    const k = 1 + 0.06 * Math.sin(a * 2 + wob[0] * 6) + 0.03 * Math.sin(a * 3 + wob[1] * 6) + 0.05 * (i / 64);
    points.push([cx + Math.cos(a) * rx * k, cy + Math.sin(a) * ry * k * (1 + wob[2] * 0.1)]);
  }
  return "M" + points.map((p) => p.map((v) => v.toFixed(1)).join(" ")).join(" L");
}

/** A marker underline under a box: slightly rising, with a hook back at the end. */
function underlinePath(b) {
  const y = b.y + b.height + 10, x0 = b.x - 8, x1 = b.x + b.width + 10;
  const points = [];
  for (let i = 0; i <= 30; i++) {
    const f = i / 30;
    points.push([x0 + (x1 - x0) * f, y - 6 * f + 2.5 * Math.sin(f * 9 + rnd())]);
  }
  points.push([x1 - 40, y + 8]);
  return "M" + points.map((p) => p.map((v) => v.toFixed(1)).join(" ")).join(" L");
}

/** A hand-drawn box round a rectangle (frame px). */
function boxPath(b) {
  const p = 14, j = () => rnd() * 8;
  const x0 = b.x - p, y0 = b.y - p, x1 = b.x + b.width + p, y1 = b.y + b.height + p;
  return \`M\${x0 + 30} \${y0 + j()} L\${x1 + j()} \${y0 + j()} L\${x1 + j()} \${y1 + j()} L\${x0 + j()} \${y1 + j()} L\${x0 + j()} \${y0 + j()} L\${x0 + 60} \${y0 - 4}\`;
}

const NS = "http://www.w3.org/2000/svg";
function stroke(svg, d, width, color) {
  const path = document.createElementNS(NS, "path");
  path.setAttribute("d", d);
  Object.assign(path.style, { fill: "none", stroke: color, strokeWidth: width, strokeLinecap: "round",
    strokeLinejoin: "round", opacity: .92 });
  svg.append(path);
  const length = path.getTotalLength();
  path.style.strokeDasharray = length;
  path.style.strokeDashoffset = length;
  return { path, length };
}

// The page's marker strokes, in the still's pixels (CSS px x 3).
const marksSvg = document.getElementById("marks");
const img = document.querySelector("#layer img");
const strokes = script.strokes.map((s) => {
  const m = S.marks[s.mark];
  const b = { x: m.x * S.SCALE, y: m.y * S.SCALE, width: m.width * S.SCALE, height: m.height * S.SCALE };
  return { ...s, ...stroke(marksSvg, s.kind === "circle" ? circlePath(b) : underlinePath(b), 9, "#B0562A") };
});

// Live clocks over the page's countdowns: each shows "start" seconds at t = 0 and counts down by the second; at a
// reload (script.reloads) it starts over from "start", as an evergreen timer does.
const pad = (n) => String(n).padStart(2, "0");
const reloads = (script.reloads ?? []).map((r) => r.at);
const clocks = (script.clocks ?? []).map((c) => {
  const m = S.marks[c.mark], el = document.createElement("div");
  el.className = "clock";
  Object.assign(el.style, { left: m.x * S.SCALE - 2 + "px", top: m.y * S.SCALE - 2 + "px",
    width: m.width * S.SCALE + 24 + "px", height: m.height * S.SCALE + 4 + "px", fontFamily: m.style.fontFamily,
    fontSize: parseFloat(m.style.fontSize) * S.SCALE + "px", fontWeight: m.style.fontWeight, color: m.style.color,
    letterSpacing: m.style.letterSpacing, fontVariantNumeric: m.style.fontVariantNumeric,
    background: m.style.background, paddingLeft: "2px" });
  document.getElementById("layer").append(el);
  return { ...c, el };
});
function clockText(c, t) {
  const since = Math.max(0, ...reloads.filter((r) => r <= t), 0);
  // The page is blank for a moment while it reloads; it comes back showing the full time.
  const left = Math.max(0, c.start - Math.floor(t - since));
  return \`\${pad(Math.floor(left / 3600))}:\${pad(Math.floor(left / 60) % 60)}:\${pad(left % 60)}\`;
}

// The check: the popup's still in the card, scaled to its width.
const card = document.getElementById("card");
const cardTop = card.offsetTop, cardLeft = card.offsetLeft, popScale = card.offsetWidth / S.W;
const f = S.popup.finding;
const findingTop = f.y * S.SCALE * popScale; // in the card, unscrolled
const scrollTo = script.check.findingY - cardTop - findingTop; // the popup's offset that puts the finding there
const boxSvg = document.getElementById("box");
let boxStroke = null;

// Captions.
const caps = document.getElementById("caps");
const captions = [...script.captions, ...script.check.captions].map((c) => {
  const el = document.createElement("div");
  el.className = "cap " + (c.tone ?? "");
  el.textContent = c.text;
  el.style.fontSize = c.size + "px";
  caps.append(el);
  return { ...c, el };
});

/** A marker stroke drawn on up to \`p\` (0 to 1); none at all before it starts (no dot of its round cap). */
function draw(s, p) {
  s.path.style.strokeDashoffset = s.length * (1 - p);
  s.path.style.visibility = p > 0 ? "visible" : "hidden";
}

function camera(t) {
  const keys = script.camera;
  if (t <= keys[0].t) return keys[0];
  for (let i = 1; i < keys.length; i++) {
    if (t <= keys[i].t) {
      const a = keys[i - 1], b = keys[i], k = ease((t - a.t) / (b.t - a.t));
      return { left: a.left + (b.left - a.left) * k, top: a.top + (b.top - a.top) * k, zoom: a.zoom + (b.zoom - a.zoom) * k };
    }
  }
  return keys[keys.length - 1];
}

const mascot = S.mascot ? (${mascotRuntime.toString()})(S.mascot) : null;

window.frame = (t) => {
  if (mascot) mascot(t);
  const c = camera(t);
  document.getElementById("layer").style.transform =
    \`scale(\${c.zoom}) translate(\${-c.left * S.SCALE}px, \${-c.top * S.SCALE}px)\`;
  for (const s of strokes) draw(s, progress(t, s.at, s.draw));
  for (const c of clocks) c.el.textContent = clockText(c, t);
  let flash = 0;
  for (const r of reloads) flash = Math.max(flash, progress(t, r, 0.12) * (1 - progress(t, r + 0.3, 0.3)));
  document.getElementById("reload").style.opacity = flash;

  // The cut to the check: it slides up over the page.
  const ch = script.check, inCheck = progress(t, ch.at, 0.45);
  document.getElementById("check").style.transform = \`translateY(\${(1 - inCheck) * 100}%)\`;
  const scrolled = scrollTo * progress(t, ch.at + 0.5, 0.8);
  card.querySelector("img").style.transform = \`translateY(\${scrolled}px)\`;
  if (!boxStroke && document.fonts.status === "loaded") {
    seed = 11;
    boxStroke = stroke(boxSvg, boxPath({ x: cardLeft + f.x * S.SCALE * popScale, y: ch.findingY,
      width: f.width * S.SCALE * popScale, height: f.height * S.SCALE * popScale }), 10, "#B0562A");
  }
  if (boxStroke) draw(boxStroke, progress(t, ch.mark, 0.7));

  const end = progress(t, script.end.at, 0.4);
  document.getElementById("end").style.opacity = end;

  for (const cap of captions) {
    const shown = progress(t, cap.from, 0.22) * (1 - progress(t, cap.to - 0.2, 0.2)) * (1 - end);
    cap.el.style.opacity = shown;
    cap.el.style.transform = \`translate(-50%, -50%) rotate(-1.2deg) scale(\${0.9 + 0.1 * shown})\`;
    cap.el.style.top = cap.y + "px";
  }
};
</script></body></html>`;

writeFileSync(join(dir, `${withMascot ? "mascot-" : ""}stage.html`), stage);
const browser = await chromium.launch({ channel: "chromium" });
try {
  const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: 1 });
  await page.goto(url(join(dir, `${withMascot ? "mascot-" : ""}stage.html`)));
  await page.evaluate(() => document.fonts.ready);
  await page.waitForLoadState("load");
  await page.evaluate(() => window.frame(0));

  if (only) {
    for (const t of only) {
      await page.evaluate((t) => window.frame(t), t);
      await page.screenshot({ path: join(dir, `${withMascot ? "mascot-" : ""}frame-${t.toFixed(2)}.png`) });
    }
    console.log(`frames in ${dir}`);
  } else {
    const mp4 = join(dir, `${output}.mp4`);
    const ffmpeg = spawn(
      FFMPEG,
      ["-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", String(FPS), "-c:v", "png", "-i", "-",
        "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-movflags", "+faststart", "-r", String(FPS), mp4],
      { stdio: ["pipe", "inherit", "inherit"] },
    );
    const done = new Promise((resolve, reject) =>
      ffmpeg.on("close", (code) => (code === 0 ? resolve() : reject(new Error(`ffmpeg exited ${String(code)}`)))),
    );
    const frames = Math.round(video.script.duration * FPS);
    for (let i = 0; i < frames; i++) {
      await page.evaluate((t) => window.frame(t), i / FPS);
      const png = await page.screenshot();
      if (!ffmpeg.stdin.write(png)) await new Promise((r) => ffmpeg.stdin.once("drain", r));
    }
    ffmpeg.stdin.end();
    await done;
    const gif = join(dir, `${output}.gif`);
    await run(FFMPEG, ["-y", "-loglevel", "error", "-i", mp4, "-vf",
      "fps=12,scale=540:-2:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];" +
        "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle", gif]);
    mkdirSync(DELIVER, { recursive: true });
    for (const file of [mp4, gif]) {
      const to = join(DELIVER, file.split("/").pop());
      copyFileSync(file, to);
      console.log(`${to} (${(statSync(to).size / 1e6).toFixed(1)} MB)`);
    }
  }
} finally {
  await browser.close();
}

/**
 * Runs a command to its end.
 *
 * @param {string} command The program.
 * @param {string[]} argv Its arguments.
 */
function run(command, argv) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, argv, { stdio: "inherit" });
    child.on("close", (code) => (code === 0 ? resolve() : reject(new Error(`${command} exited ${String(code)}`))));
  });
}
