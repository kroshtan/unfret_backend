// The Unfret mascot as an animated character on a video's stage: one character with two poses, C ("Leaning in",
// facing left, the glass held up beside it) and A ("Close inspection", turned to the right, the glass in front of its
// right eye), and everything between. The rigged drawing is assets/unfret-mascot.svg; a video's script gives it a
// `mascot` track (videos.mjs) and render.mjs, run with --mascot, poses it per frame from that track.
//
// The track: `scale` (frame px per logo unit; the figure is about 146 units tall), `dark` (the colours for a dark
// background), `behind` ([{ from, to, id }]: in that time the mascot is under the element with that id, e.g. so a
// scene slides in over it; otherwise it is on top of everything), and
//   keys   [{ t, ease?, x, y, pose, lean, mag, reach, hand, lens, look, track, flat, grin, brow, wide }]
//          Each channel eases from the last key that sets it to the next (ease: inout, in, out, linear, back, spring;
//          the later key's). x, y: where the feet stand, in frame px. pose: 0 is C, 1 is A. lean: degrees, on top of
//          the pose's own tilt (negative leans left). mag: the magnifier's turn from the pose's, in degrees. reach:
//          0 to 1, the shoulder rising for a raised arm. hand: "rest" (the pose's), or lens: where the glass's centre
//          should be, in frame px. look: the eyes' offset from the pose's, in logo units; track: 0 to 1, the eyes
//          following the glass instead. flat, grin: 0 to 1, the check-smile flattened or opened into a grin. brow:
//          0 to 1, a raised brow (over the eye that isn't behind the glass). wide: 0 to 1, the eyes widened.
//   hops   [{ at, dur, height }]  a hop between two keys' x, y (set ease "linear" on the landing key): crouch
//          before, stretch in the air, a damped squash after landing; height in frame px.
//   blinks [t]                    nods [{ at, dur?, depth? }]   waves [{ at, dur, amp }]  (a waggle of the glass)
//   become { at, dur, logo, then, arc? }  it turns into the logo: from `at`, over `dur`, it moves into the box of the
//          element `logo` (a CSS selector; that element is hidden, the mascot takes its place), its glass fades, its
//          arms and legs draw into the tile, it straightens and its face settles into the logo's; then it holds
//          still. The elements `then` (a selector) fade in after. arc: a rise on the way, in frame px.
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { HERE } from "./videos.mjs";

export const MASCOT_SVG = join(HERE, "assets", "unfret-mascot.svg");

// The colours for a dark background: the deep-green limbs and the ink magnifier would vanish there, so the limbs go
// mint and the magnifier white; the glass is a faint white and the ground shadow black. The glass's shadow on the
// tile stays deep green. (The same set as media/mascot's -dark files.)
const DARK = [
  [/(stroke|fill)="#0F5C4A"/g, '$1="#E1F1EA"'],
  [/stroke="#12302A"/g, 'stroke="#FFFFFF"'],
  [/fill="#12302A" opacity="0.14"/, 'fill="#000000" opacity="0.3"'],
  [/fill="#E1F1EA" opacity="0.55"/, 'fill="#FFFFFF" opacity="0.16"'],
];

/**
 * The rigged drawing, in its light or dark colours.
 *
 * @param {boolean} [dark] The dark-background colours.
 * @returns {string} The SVG file's text.
 */
export function mascotSvg(dark = false) {
  const svg = readFileSync(MASCOT_SVG, "utf8");
  if (!dark) return svg;
  return svg
    .split("\n")
    .map((line) => (line.includes("m-lens-shadow") ? line : DARK.reduce((text, [from, to]) => text.replace(from, to), line)))
    .join("\n");
}

/**
 * The mascot's markup for the stage: the rigged drawing in a frame-sized SVG layer on top of everything.
 *
 * @param {{dark?: boolean}} track The video's mascot track.
 * @param {number} width The frame's width.
 * @param {number} height The frame's height.
 * @returns {string} The markup.
 */
export function mascotMarkup(track, width, height) {
  const svg = mascotSvg(track.dark);
  const inner = svg.slice(svg.indexOf(">", svg.indexOf("<svg")) + 1, svg.lastIndexOf("</svg>"));
  return `<svg id="mascot" xmlns="http://www.w3.org/2000/svg"
    width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"
    style="position:absolute;left:0;top:0;overflow:visible;pointer-events:none">${inner}</svg>`;
}

/**
 * The stage's side: builds the pose function from a track. Self-contained, as render.mjs puts its source into the
 * stage page (`(${mascotRuntime})(track)`), so it may use nothing from this module.
 *
 * @param {object} track The video's mascot track.
 * @returns {(t: number) => void} Poses the mascot at time t (seconds).
 */
export function mascotRuntime(track) {
  const $ = (id) => document.getElementById(id);
  const el = { root: $("mascot"), place: $("m-place"), lift: $("m-lift"), squash: $("m-squash"),
    shadow: $("m-shadow"), arm: $("m-arm"), legL: $("m-leg-l"), legR: $("m-leg-r"), body: $("m-body"),
    eyeL: $("m-eye-l"), eyeR: $("m-eye-r"), brow: $("m-brow"), smile: $("m-smile"), mag: $("m-mag"),
    lensWorld: $("m-lens-world"), lensClip: $("m-lens-clip-shape"), glass: $("m-glass"), ring: $("m-ring"),
    handle: $("m-handle"), glare: $("m-glare"), tileClip: $("m-tile-clip-rect"), lensShadow: $("m-lens-shadow"),
    shadowRing: $("m-lens-shadow-ring"), shadowHandle: $("m-lens-shadow-handle") };

  // The two poses' geometry, in logo units. Points on the tile are in its own unturned units.
  const C = {
    tilt: -8, // the tile's turn about (64, 64)
    pivot: [70, 120], // the lean's pivot, at the hips
    eyes: [[39, 44], [73, 44]],
    hips: [[44, 118], [82, 118]],
    soles: [[54.2, 138], [92.58, 138]], // the legs run along the tilt to here; flat soles from here
    toe: -10, // the soles point left
    shoulder: [7.5, 92.41], // hidden behind the tile
    shoulderUp: [24, 52], // with reach
    bend: 0.5, // the elbow's sag, by the arm's length
    hand: [-18.86, 94.23],
    mag: 10, // the magnifier's turn (0: the glass straight above the hand)
    reachLen: 51, // hand to the glass's centre
    lens: [19, 19, 0], // the glass: rx, ry, its turn
    zoom: [1.55, 0, 0], // the glass's magnification, about this point from its centre
    handleFrom: 21, // the handle starts this far from the glass's centre
    glare: [-11.74, -1.03, -5.9, -10.2, 11.78, 11.78, 0], // from, to (from the centre), rx, ry, turn
    shadow: [70, 0], // the ground shadow's centre x; the glass's shadow on the tile (A only)
  };
  const A = {
    tilt: 10,
    pivot: [57, 120],
    eyes: [[57, 42], [91, 42]],
    hips: [[45.63, 121.06], [84.27, 121.35]],
    soles: [[32.3, 138], [71.5, 138]],
    toe: 10,
    shoulder: [125.4, 89.73],
    shoulderUp: [112, 50],
    bend: 0.3,
    hand: [154.78, 69.76],
    mag: -55,
    reachLen: 57.1,
    lens: [20, 16.8, 20],
    zoom: [1.45, -34.27, -15.91],
    handleFrom: 19.1,
    glare: [11.59, -2.15, 10.34, 6.62, 12.4, 10.42, 20],
    shadow: [57, 0.35],
  };
  const G = [70, 138]; // the ground point: where x, y put the figure
  const SMILE = { check: [31, 70, 50, 89, 58, 96, 66, 89, 99, 57], flat: [38, 79, 50, 79, 58, 79, 66, 79, 90, 79],
    grin: [27, 63, 50, 93, 58, 101, 66, 93, 104, 50] };
  const k = track.scale;
  const shadowAlpha = Number(el.shadow.getAttribute("opacity"));

  // 2D affine matrices [a, b, c, d, e, f], as SVG's matrix().
  const mul = (m, n) => [m[0] * n[0] + m[2] * n[1], m[1] * n[0] + m[3] * n[1], m[0] * n[2] + m[2] * n[3],
    m[1] * n[2] + m[3] * n[3], m[0] * n[4] + m[2] * n[5] + m[4], m[1] * n[4] + m[3] * n[5] + m[5]];
  const T = (x, y) => [1, 0, 0, 1, x, y];
  const S = (sx, sy, c = [0, 0]) => mul(T(c[0], c[1]), mul([sx, 0, 0, sy, 0, 0], T(-c[0], -c[1])));
  const R = (deg, c = [0, 0]) => {
    const a = (deg * Math.PI) / 180, cos = Math.cos(a), sin = Math.sin(a);
    return mul(T(c[0], c[1]), mul([cos, sin, -sin, cos, 0, 0], T(-c[0], -c[1])));
  };
  const inv = (m) => {
    const det = m[0] * m[3] - m[1] * m[2];
    return [m[3] / det, -m[1] / det, -m[2] / det, m[0] / det, (m[2] * m[5] - m[3] * m[4]) / det,
      (m[1] * m[4] - m[0] * m[5]) / det];
  };
  const ap = (m, p) => [m[0] * p[0] + m[2] * p[1] + m[4], m[1] * p[0] + m[3] * p[1] + m[5]];
  const css = (m) => `matrix(${m.map((v) => v.toFixed(4)).join(" ")})`;
  const n2 = (v) => v.toFixed(2);
  const set = (node, attrs) => { for (const name in attrs) node.setAttribute(name, attrs[name]); };

  // Easing.
  const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
  const EASE = {
    linear: (x) => x,
    in: (x) => x * x * x,
    out: (x) => 1 - Math.pow(1 - x, 3),
    inout: (x) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2),
    back: (x) => 1 + 2.70158 * Math.pow(x - 1, 3) + 1.70158 * Math.pow(x - 1, 2),
    spring: (x) => 1 - Math.exp(-6 * x) * Math.cos(x * 9.5),
  };
  const bump = (x) => (x <= 0 || x >= 1 ? 0 : Math.sin(Math.PI * x)); // 0 -> 1 -> 0
  const smooth = (x) => { x = clamp(x); return x * x * (3 - 2 * x); };
  const lerp = (a, b, p) => (Array.isArray(a) ? a.map((v, i) => v + (b[i] - v) * p) : a + (b - a) * p);
  /** The geometry between the poses: C at 0, A at 1. */
  const between = (p) => {
    const out = {};
    for (const name in C) out[name] = lerp(C[name], A[name], p);
    out.hips = C.hips.map((h, i) => lerp(h, A.hips[i], p));
    out.soles = C.soles.map((h, i) => lerp(h, A.soles[i], p));
    out.eyes = C.eyes.map((h, i) => lerp(h, A.eyes[i], p));
    return out;
  };

  const DEFAULT = { pose: 0, lean: 0, mag: 0, reach: 0, hand: [0, 0], look: [0, 0], track: 0, flat: 0, grin: 0,
    brow: 0, wide: 0 };
  const keys = track.keys.map((key) => ({ ...key })).sort((a, b) => a.t - b.t);
  function channel(name, t) {
    let prev = null;
    for (const key of keys) {
      if (key[name] === undefined) continue;
      if (key.t >= t) {
        if (!prev) return key[name];
        const p = (t - prev.t) / (key.t - prev.t || 1);
        return lerp(prev[name], key[name], EASE[key.ease ?? "inout"](clamp(p)));
      }
      prev = key;
    }
    return prev ? prev[name] : DEFAULT[name];
  }
  // The hand channel is the hand's offset from the pose's own. A key's lens position (frame px) becomes the hand
  // that puts the glass there, with the figure where it stands and as it is posed then.
  for (const key of keys) {
    if (key.hand === "rest") key.hand = [0, 0];
    if (!key.lens) continue;
    const x = key.x ?? channel("x", key.t), y = key.y ?? channel("y", key.t);
    const g = between(key.pose ?? channel("pose", key.t)), mag = g.mag + (key.mag ?? channel("mag", key.t));
    const at = [G[0] + (key.lens[0] - x) / k, G[1] + (key.lens[1] - y) / k];
    const off = ap(R(mag), [0, -g.reachLen]);
    key.hand = [at[0] - off[0] - g.hand[0], at[1] - off[1] - g.hand[1]];
  }

  /** A hop's lift (frame px) and squash (+) or stretch (-) at time t. */
  function hops(t) {
    let lift = 0, squash = 0;
    for (const h of track.hops ?? []) {
      const p = (t - h.at) / h.dur;
      if (p > 0 && p < 1) {
        lift += h.height * 4 * p * (1 - p);
        squash -= 0.09 * bump(p < 0.5 ? p * 2 : (1 - p) * 2) * (1 - 0.5 * bump(p));
      }
      const before = (t - (h.at - 0.14)) / 0.14; // the crouch
      if (before > 0 && before < 1) squash += 0.1 * bump(before);
      const after = t - (h.at + h.dur); // the landing
      if (after > 0 && after < 0.6) squash += 0.14 * Math.exp(-after / 0.1) * Math.cos((after * 2 * Math.PI) / 0.34);
    }
    return { lift, squash };
  }

  // Turning into the logo: the logo's element is hidden for good, its box is where the tile ends up (the tile's
  // units are the logo's own, 128 to its side).
  const become = track.become ?? null;
  let logoBox = null, thenEls = [];
  if (become) {
    const logo = document.querySelector(become.logo);
    logo.style.visibility = "hidden";
    thenEls = [...document.querySelectorAll(become.then ?? ":not(*)")];
    const box = () => {
      const r = logo.getBoundingClientRect(), root = el.root.getBoundingClientRect();
      return mul(T(r.left - root.left, r.top - root.top), S(r.width / 128, r.height / 128));
    };
    logoBox = box;
  }

  /** Puts the mascot under an element for a while (a scene sliding in over it), else on top of everything. */
  function layer(t) {
    const spot = (track.behind ?? []).find((b) => t >= b.from && t < b.to);
    const before = spot ? $(spot.id) : null;
    if (before && el.root.nextSibling !== before) before.parentNode.insertBefore(el.root, before);
    if (!before && el.root.parentNode.lastElementChild !== el.root) el.root.parentNode.append(el.root);
  }

  return (t) => {
    layer(t);
    // b: how far it has turned into the logo (0: not at all, 1: it is the logo). Everything that isn't the logo
    // fades out with (1 - b); `early` goes first, so the glass and limbs are gone before the face settles.
    const b = become ? EASE.inout(clamp((t - become.at) / become.dur)) : 0;
    const keep = 1 - b, early = 1 - smooth(b / 0.6);
    for (const node of thenEls) {
      node.style.opacity = String(become ? smooth((t - become.at - become.dur) / 0.4) : 1);
    }
    const x = channel("x", t), y = channel("y", t), pose = clamp(channel("pose", t)), g = between(pose);
    let lean = channel("lean", t) * keep, mag = g.mag + channel("mag", t), look = lerp(channel("look", t), [0, 0], b);
    const reach = channel("reach", t), handOff = channel("hand", t), trackLens = channel("track", t) * keep;
    const flat = channel("flat", t) * keep, grin = channel("grin", t) * keep, brow = channel("brow", t) * early;
    const wide = channel("wide", t) * keep;
    let bob = 0.8 * Math.sin((t * 2 * Math.PI) / 2.6) * keep; // breathing
    const breath = 0.006 * Math.sin((t * 2 * Math.PI) / 2.6 + 0.6) * keep;

    for (const nod of track.nods ?? []) {
      const p = bump((t - nod.at) / (nod.dur ?? 0.4)) * (nod.depth ?? 1);
      lean += 7 * p * (pose < 0.5 ? -1 : 1); // toward where it faces
      bob += 2.5 * p;
      look = [look[0], look[1] + 2 * p];
    }
    for (const w of track.waves ?? []) {
      const p = (t - w.at) / w.dur;
      if (p > 0 && p < 1) mag += w.amp * Math.sin(p * 2 * Math.PI * (w.cycles ?? 2)) * bump(p);
    }
    let blink = 0;
    for (const b of track.blinks ?? []) {
      const q = (t - b) / 0.2;
      if (q > 0 && q < 1) blink = Math.max(blink, q < 0.4 ? smooth(q / 0.4) : 1 - smooth((q - 0.4) / 0.6));
    }
    blink *= keep;

    const hop = hops(t), lift = hop.lift * keep + (become?.arc ?? 0) * bump(b), squash = hop.squash * keep;
    const place = mul(T(x, y), mul(S(k, k), T(-G[0], -G[1])));
    el.place.setAttribute("transform", css(b > 0 ? lerp(place, logoBox(), b) : place));
    el.lift.setAttribute("transform", `translate(0 ${n2(-lift / k)})`);
    el.squash.setAttribute("transform", css(S(1 + 0.6 * squash, 1 - squash, G)));
    const air = clamp(lift / 220);
    set(el.shadow, { cx: n2(g.shadow[0]), rx: n2(48 * (1 - 0.45 * air) * (1 + 0.4 * Math.max(0, squash))) });
    el.shadow.setAttribute("opacity", (shadowAlpha * (1 - 0.6 * air) * early).toFixed(3));

    // The tile: the pose's tilt, the lean at the hips, breathing.
    const body = mul(T(0, bob), mul(R(lean, g.pivot), mul(R(g.tilt * keep, [64, 64]), S(1 - breath, 1 + breath, [64, 124]))));
    el.body.setAttribute("transform", css(body));
    el.tileClip.setAttribute("transform", css(body));

    // Legs: hip (on the tile) to the planted sole, the toes turning with the pose.
    [el.legL, el.legR].forEach((leg, i) => {
      const hip = ap(body, g.hips[i]), sole = lerp(hip, g.soles[i], early); // drawn into the tile as it becomes the logo
      leg.setAttribute("d", `M${n2(hip[0])} ${n2(hip[1])} L${n2(sole[0])} ${n2(sole[1])} H${n2(sole[0] + g.toe * early)}`);
    });

    // The magnifier at the hand: its glass, ring, handle and glare between the poses' shapes.
    const shoulder = ap(body, lerp([48, 90], lerp(g.shoulder, g.shoulderUp, reach), early)); // well inside, as a logo
    const hand = lerp(shoulder, [g.hand[0] + handOff[0], g.hand[1] + handOff[1]], early);
    const magM = mul(T(hand[0], hand[1]), mul(R(mag), S(0.6 + 0.4 * early, 0.6 + 0.4 * early)));
    el.mag.setAttribute("transform", css(magM));
    el.mag.setAttribute("opacity", early.toFixed(3));
    const cy = -g.reachLen, [rx, ry, turn] = g.lens;
    const ellipse = { cx: "0", cy: n2(cy), transform: `rotate(${n2(turn)} 0 ${n2(cy)})` };
    set(el.lensClip, { ...ellipse, rx: n2(rx), ry: n2(ry) });
    set(el.glass, { ...ellipse, rx: n2(rx), ry: n2(ry) });
    set(el.ring, { ...ellipse, rx: n2(rx + 4), ry: n2(ry + 4) });
    const handle = `M0 ${n2(cy + g.handleFrom)} L0 0`;
    el.handle.setAttribute("d", handle);
    const [gx1, gy1, gx2, gy2, grx, gry, gturn] = g.glare;
    el.glare.setAttribute("d", `M${n2(gx1)} ${n2(cy + gy1)} A${n2(grx)} ${n2(gry)} ${n2(gturn)} 0 1 ${n2(gx2)} ${n2(cy + gy2)}`);
    const lensAt = ap(magM, [0, cy]);
    const zoomAt = ap(magM, [g.zoom[1], cy + g.zoom[2]]);
    el.lensWorld.setAttribute("transform", css(mul(inv(magM), S(g.zoom[0], g.zoom[0], zoomAt))));
    // Its shadow on the tile (A): the ring and handle, down and to the left.
    el.lensShadow.setAttribute("opacity", (g.shadow[1] * early).toFixed(3));
    if (g.shadow[1] > 0.001) {
      el.lensShadow.setAttribute("transform", css(mul(T(-6, 7), magM)));
      set(el.shadowRing, { ...ellipse, rx: n2(rx + 4), ry: n2(ry + 4) });
      el.shadowHandle.setAttribute("d", handle);
    }

    // The arm, from the shoulder behind the tile to the hand, with a soft elbow.
    const d = [hand[0] - shoulder[0], hand[1] - shoulder[1]], len = Math.hypot(d[0], d[1]) || 1;
    let perp = [-d[1] / len, d[0] / len];
    if (perp[1] < 0) perp = [-perp[0], -perp[1]];
    const bend = g.bend * clamp(len, 16, 50);
    const mid = [(shoulder[0] + hand[0]) / 2 + perp[0] * bend, (shoulder[1] + hand[1]) / 2 + perp[1] * bend];
    el.arm.setAttribute("d", `M${n2(shoulder[0])} ${n2(shoulder[1])} Q${n2(mid[0])} ${n2(mid[1])} ${n2(hand[0])} ${n2(hand[1])}`);

    // Eyes: the pose's, offset by look or toward the glass; widened; blinking.
    if (trackLens > 0) {
      const eyesAt = ap(body, [(g.eyes[0][0] + g.eyes[1][0]) / 2, g.eyes[0][1]]);
      const v = [lensAt[0] - eyesAt[0], lensAt[1] - eyesAt[1]], vl = Math.hypot(v[0], v[1]) || 1;
      const a = (-(lean + g.tilt) * Math.PI) / 180; // into the tile's own units
      const local = [(v[0] * Math.cos(a) - v[1] * Math.sin(a)) / vl, (v[0] * Math.sin(a) + v[1] * Math.cos(a)) / vl];
      look = lerp(look, [local[0] * 5.5, local[1] * 5.5], trackLens);
    }
    const ll = Math.hypot(look[0], look[1]);
    if (ll > 6.5) look = [(look[0] * 6.5) / ll, (look[1] * 6.5) / ll];
    const r = 7.5 * (1 + 0.28 * wide);
    const LOGO_EYES = [[45, 44], [79, 44]];
    [el.eyeL, el.eyeR].forEach((eye, i) => {
      const at = lerp(g.eyes[i], LOGO_EYES[i], b);
      set(eye, { cx: n2(at[0] + look[0]), cy: n2(at[1] + look[1]), rx: n2(r * (1 + 0.08 * blink)),
        ry: n2(Math.max(0.9, r * (1 - 0.88 * blink))) });
    });

    // The brow, over the eye that isn't behind the glass: pops up and rises.
    const eye = g.eyes[pose < 0.5 ? 1 : 0];
    const bx = eye[0] + look[0], by = eye[1] + look[1] - r - 8 - 4 * brow;
    el.brow.setAttribute("d", `M${n2(bx - 8)} ${n2(by + 3)} Q${n2(bx)} ${n2(by - 3)} ${n2(bx + 8)} ${n2(by + 1.5)}`);
    el.brow.setAttribute("opacity", clamp(brow * 4).toFixed(3));
    el.brow.setAttribute("transform", css(S(Math.max(0.01, clamp(brow * 1.5)), Math.max(0.01, clamp(brow * 1.5)), [bx, by])));

    // The check-smile, morphed.
    const s = SMILE.check.map((v, i) => v + (SMILE.flat[i] - v) * flat + (SMILE.grin[i] - v) * grin);
    el.smile.setAttribute("d", `M${n2(s[0])} ${n2(s[1])} L${n2(s[2])} ${n2(s[3])} Q${n2(s[4])} ${n2(s[5])} ${n2(s[6])} ${n2(s[7])} L${n2(s[8])} ${n2(s[9])}`);
  };
}

// `node mascot.mjs <out.svg> [dark]`: writes pose C at rest as a still (no ground shadow), e.g. for the brand kit.
if (process.argv[1] === fileURLToPath(import.meta.url) && process.argv[2]) {
  const still = mascotSvg(process.argv[3] === "dark").split("\n").filter((line) => !line.includes('id="m-shadow"'));
  writeFileSync(process.argv[2], still.join("\n"));
}
