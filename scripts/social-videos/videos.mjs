// The social videos: one trick each, the page it's on, what the real check on that page says about it, and the
// script (scenes, callouts, captions) the frames are rendered from (render.mjs).
//
// Times are in seconds. Page coordinates are CSS pixels of the 360 px wide phone viewport; the frame is 1080x1920
// (3x). Captions stay between y 200 and 1670 of the frame, clear of the platforms' own buttons.
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export const HERE = dirname(fileURLToPath(import.meta.url));
// Everything generated (results, stills, frames, videos) goes here, out of git.
export const OUT = join(HERE, "out");
// The extension checkout, a sibling of the backend's.
export const EXTENSION = process.env.UNFRET_EXTENSION ?? join(HERE, "..", "..", "..", "unfret_extension");
// Where the finished MP4s and GIFs are copied: the metarepo's media/videos, beside the checkouts (out of git).
export const DELIVER = process.env.UNFRET_VIDEOS_DIR ?? join(HERE, "..", "..", "..", "media", "videos");

// The real pages behind the videos, kept out of this public repo (a shop isn't named next to its trick): per video,
// its `url` and the brand's words to blur on the page and in the popup (`brand`). By default in the metarepo's
// media/videos/pages.json: {"c-piece-count": {"url": "https://…", "brand": ["…"]}, …}.
const PAGES_FILE = process.env.UNFRET_VIDEO_PAGES ?? join(DELIVER, "pages.json");
const PAGES = existsSync(PAGES_FILE) ? JSON.parse(readFileSync(PAGES_FILE, "utf8")) : {};

/**
 * A mascot track (mascot.mjs) in the videos' usual two appearances, from a few beats.
 *
 * The close-up: from `enter`, it hops in from the right as C along the bottom of the frame to `spot` (feet, frame
 * px), turns into A (close inspection) and raises the glass toward `look` (frame px; the eyes follow it), puts it back
 * to its eye, does a double-take at `doubleTake` and nods at each of `nods`; the check then slides in over it. The
 * check (optional, `check: { spot, lens }`): it hops in as C to `spot` and holds the glass up at `lens`. The end card:
 * it becomes the logo (from the check's spot, or hopping in from the right when there is no check appearance).
 *
 * @param {object} beats The beats.
 * @returns {object} The track.
 */
function mascotTrack({ enter, spot, look, doubleTake, nods = [], blinks = [], check = null, turn = true, cut = 13.2,
  end = 19.75 }) {
  const [x, y] = spot;
  const pose = turn ? 1 : 0, side = turn ? 1 : -1; // A looks right, C left
  const keys = [
    { t: 0, x: 1300, y: y - 10, pose: 0, look: [-3, 0], lean: 0, mag: 0, reach: 0, hand: "rest", track: 0, flat: 0,
      brow: 0, wide: 0, grin: 0 },
    // Hops in as C, then turns into A, the glass up to its eye.
    { t: enter, x: 1300, y: y - 10 },
    { t: enter + 0.42, ease: "linear", x: (1300 + x) / 2 + 40, y: y - 5 },
    { t: enter + 0.9, ease: "linear", x, y },
    { t: enter + 1.05, pose: 0, look: [-3, 0] },
    { t: enter + 1.4, pose, look: [2 * side, -3] },
    // The glass toward the thing it inspects, eyes following, and back to the eye.
    { t: enter + 1.5, hand: "rest", mag: 0, reach: 0, lean: 0, track: 0 },
    { t: enter + 2.0, lens: look, mag: 25 * side, reach: 0.8, lean: 6 * side, track: 1 },
    { t: enter + 2.6, lens: [look[0] + 40 * side, look[1] - 10], mag: 25 * side, reach: 0.8, lean: 7 * side, track: 1 },
    { t: doubleTake - 0.3, ease: "out", hand: "rest", mag: 0, reach: 0, lean: 0, track: 0, look: [3 * side, -2],
      flat: 0, brow: 0, wide: 0 },
    // The double-take.
    { t: doubleTake - 0.05, look: [-2 * side, -3], wide: 0, brow: 0, flat: 0, lean: 0 },
    { t: doubleTake + 0.18, ease: "back", look: [3 * side, -3], wide: 1, brow: 1, flat: 1, lean: -5 * side },
    { t: doubleTake + 1.5, look: [3 * side, -3], wide: 1, brow: 1, flat: 1, lean: -5 * side },
    { t: doubleTake + 1.85, ease: "out", look: [3 * side, -1], wide: 0, brow: 0, flat: 0, lean: 0 },
    // Out of sight under the check: back to C, off to the right.
    { t: cut + 0.5, x, y, pose, look: [3 * side, -1] },
    { t: cut + 0.52, ease: "linear", x: 1300, y: check ? check.spot[1] : 815, pose: 0, look: [-3, 0] },
  ];
  const hops = [
    { at: enter, dur: 0.42, height: 60 },
    { at: enter + 0.47, dur: 0.43, height: 50 },
    { at: enter + 1.12, dur: 0.26, height: 16 },
    { at: doubleTake + 0.04, dur: 0.2, height: 14 },
  ];
  const allNods = nods.map((at) => ({ at }));
  if (check) {
    const [cx, cy] = check.spot;
    keys.push(
      { t: cut + 1.3, x: 1300, y: cy },
      { t: cut + 1.75, ease: "linear", x: cx, y: cy },
      { t: cut + 1.8, look: [-3, 0], hand: "rest", mag: 0, reach: 0, lean: 0 },
      { t: cut + 2.25, lens: check.lens, mag: -22, reach: 0.5, lean: -4, look: [-5, -5] },
      { t: end - 0.45, lens: check.lens, mag: -22, reach: 0.5, lean: -4, look: [-5, -5] },
      { t: end - 0.05, ease: "out", hand: "rest", mag: 0, reach: 0, lean: 0, look: [-3, 0] },
    );
    hops.push({ at: cut + 1.3, dur: 0.45, height: 60 });
    allNods.push({ at: cut + 2.55, depth: 0.6 });
  } else {
    // No check appearance: it hops in on the end card, on its way to the logo.
    keys.push({ t: end, x: 1300, y: 815 }, { t: end + 0.5, ease: "linear", x: 900, y: 815 });
    hops.push({ at: end, dur: 0.5, height: 50 });
  }
  const becomeAt = check ? end : end + 0.3;
  return {
    scale: 1.65,
    behind: [{ from: 0, to: cut + 0.55, id: "check" }],
    keys,
    hops,
    blinks: [enter + 1.7, ...blinks],
    nods: allNods,
    become: { at: becomeAt, dur: 1.05, logo: "#end img", then: "#end .name, #end .line, #end .web, #end .free", arc: 40 },
  };
}

// The test shop on dev, and its pop-ups ("someone just bought this") that would cover the page in a still.
const TEST_SHOP = "https://dev.unfret.net/test-shop";
const SHOP_POPS = ["[data-sales-pop]"];

const VIDEO_LIST = [
  {
    name: "c-piece-count",
    // A real Amazon.nl listing of a felt-tip pen set (in pages.json): "91-delig" (91 pieces), "80 viltstiften en 12
    // clips".
    // Text the real result must contain: the finding the video points at.
    expect: "make 92, not 91",
    page: {
      locale: "nl-NL",
      height: 1500,
      dismiss: ["#sp-cc-rejectall-link"],
      // The platform's header (its app banner, logo, search bar and the delivery town), its "choice" badge, a
      // sponsored ad.
      hide: ["#nav-main", "#acBadge_feature_div", "#mobile-dp-ilm_feature_div_0"],
      // The brand's byline.
      blur: ["#bylineInfo"],
      // The platform wherever it names itself (and the brand's words, from pages.json).
      blurText: ["Amazon"],
      // The brand's name printed on the box in the main photo.
      blurBoxes: [{ selector: "#image-block", left: 0.25, top: 0.47, width: 0.48, height: 0.14 }],
      marks: {
        count: { selector: "#title", text: "91-delig" },
        parts: { selector: "#title_feature_div", text: "80 viltstiften en 12 clips" },
      },
    },
    popup: {
      // The finding the video highlights, as the popup shows it.
      finding: "make 92, not 91",
      blurText: [],
    },
    // The script. The page scene shows page.png in a phone browser; the camera keys put page point (left, top) (CSS
    // px) at the view's top-left corner, zoomed by `zoom`, and ease between keys.
    script: {
      duration: 23,
      camera: [
        { t: 0.0, left: 0, top: 0, zoom: 1 },
        { t: 1.0, left: 0, top: 0, zoom: 1 },
        { t: 3.4, left: 0, top: 400, zoom: 1 },
        { t: 4.4, left: 0, top: 400, zoom: 1 },
        { t: 5.8, left: 0, top: 0, zoom: 1 },
        { t: 7.0, left: 4, top: 8, zoom: 1.9 },
        { t: 13.2, left: 4, top: 8, zoom: 1.9 },
      ],
      // Marker strokes on the page, drawn on over `draw` seconds: a loose circle round a mark, or an underline.
      strokes: [
        { mark: "count", kind: "circle", at: 7.3, draw: 0.6 },
        { mark: "parts", kind: "underline", at: 9.0, draw: 0.6 },
      ],
      // Captions on the page scene: `y` is the caption's centre in the frame.
      captions: [
        { from: 0.4, to: 4.5, y: 1240, size: 78, text: "A 91-piece pen set." },
        { from: 4.7, to: 7.0, y: 1240, size: 78, text: "Now read the title." },
        { from: 7.6, to: 13.2, y: 1130, size: 100, text: "91 pieces?" },
        { from: 9.4, to: 13.2, y: 1290, size: 80, text: "80 pens + 12 clips = 92", tone: "caution" },
        { from: 11.0, to: 13.2, y: 1430, size: 54, text: "And 12 of the “pieces” are clips.", tone: "soft" },
      ],
      // The cut to Unfret: the popup with the real result, scrolled so the finding's top sits at `findingY` (frame
      // px), and a marker box drawn round it at `mark`.
      check: {
        at: 13.2,
        findingY: 860,
        mark: 14.6,
        captions: [
          { from: 13.5, to: 19.8, y: 300, size: 66, text: "Unfret checks the same page" },
          { from: 15.4, to: 19.8, y: 1560, size: 60, text: "It adds up the parts: 92, not 91", tone: "caution" },
        ],
      },
      // The end card, to the end.
      end: { at: 19.8 },
      // The mascot (render.mjs --mascot; the track's format is in mascot.mjs), in two appearances. The title close-up:
      // it hops in from the right along the white band under the title (as C, "Leaning in"), turns into A ("Close
      // inspection") at the left, under the title and left of "91 pieces?" (feet at 160, 1105), reads the title with
      // the glass, does a double-take at "91 pieces?" and nods at the sum; the check slides in over it. The check: C
      // hops in to the card's lower right (870, 1498) and holds the glass up to the finding; as the end card comes, it
      // rises into the logo's place and turns into the logo.
      mascot: {
        scale: 1.65,
        behind: [{ from: 0, to: 13.75, id: "check" }],
        keys: [
          { t: 0, x: 1300, y: 1052, pose: 0, look: [-3, 0], lean: 0, mag: 0, reach: 0, hand: "rest", track: 0, flat: 0,
            brow: 0, wide: 0, grin: 0 },
          // "Now read the title": hops in as C while the camera goes up to the title.
          { t: 5.2, x: 1300, y: 1052 },
          { t: 5.62, ease: "linear", x: 720, y: 1072 },
          { t: 6.1, ease: "linear", x: 160, y: 1105 },
          // Turns into A, the glass up to its eye, looking up at the title.
          { t: 6.25, pose: 0, look: [-3, 0] },
          { t: 6.6, pose: 1, look: [0, -3] },
          // Reads the title with the glass, eyes following it.
          { t: 6.65, hand: "rest", mag: 0, reach: 0, lean: 0, track: 0 },
          { t: 6.98, lens: [140, 822], mag: 40, reach: 1, lean: 5, track: 1 },
          { t: 7.5, lens: [270, 822], mag: 40, reach: 1, lean: 6, track: 1 },
          // "91 pieces?" pops up on its right: the glass back to the eye, a glance, a double-take.
          { t: 7.78, ease: "out", hand: "rest", mag: 0, reach: 0, lean: 0, track: 0, look: [3, 3], flat: 0, brow: 0,
            wide: 0 },
          { t: 7.92, look: [-2, -3], wide: 0, brow: 0, flat: 0, lean: 0 },
          { t: 8.12, ease: "back", look: [3, 3], wide: 1, brow: 1, flat: 1, lean: -5 },
          { t: 9.6, look: [3, 3], wide: 1, brow: 1, flat: 1, lean: -5 },
          // "80 pens + 12 clips = 92", below: got it; nods.
          { t: 9.95, ease: "out", look: [2, 4], wide: 0, brow: 0, flat: 0, lean: 0 },
          { t: 11.2, look: [3, 5] },
          // Under the check now; out of sight it goes back to C, off to the right.
          { t: 13.7, x: 160, y: 1105, pose: 1, look: [3, 5] },
          { t: 13.72, ease: "linear", x: 1300, y: 1498, pose: 0, look: [-3, 0] },
          // The check: hops in to the card's corner, holds the glass up to the finding.
          { t: 14.5, x: 1300, y: 1498 },
          { t: 14.95, ease: "linear", x: 870, y: 1498 },
          { t: 15.0, look: [-3, 0], hand: "rest", mag: 0, reach: 0, lean: 0 },
          { t: 15.45, lens: [708, 1306], mag: -22, reach: 0.5, lean: -4, look: [-5, -5] },
          { t: 19.3, lens: [708, 1306], mag: -22, reach: 0.5, lean: -4, look: [-5, -5] },
          // The end card: lowers the glass, then turns into the logo (become, below).
          { t: 19.7, ease: "out", hand: "rest", mag: 0, reach: 0, lean: 0, look: [-3, 0] },
        ],
        hops: [
          { at: 5.2, dur: 0.42, height: 60 },
          { at: 5.67, dur: 0.43, height: 50 },
          { at: 6.32, dur: 0.26, height: 16 },
          { at: 7.98, dur: 0.2, height: 14 },
          { at: 14.5, dur: 0.45, height: 60 },
        ],
        blinks: [6.9, 8.9, 11.7, 12.7, 16.6, 18.3],
        nods: [{ at: 10.05 }, { at: 10.5 }, { at: 15.75, depth: 0.6 }],
        // As the end card fades in it rises into the logo's place and becomes the logo; the words come after.
        become: { at: 19.75, dur: 1.05, logo: "#end img", then: "#end .name, #end .line, #end .web, #end .free", arc: 40 },
      },
    },
  },
  // Real pages (addresses in pages.json): each video one trick, on a page where Unfret's deep check on dev shows it on
  // the card today.
  {
    name: "f-was-price",
    // A Czech fashion shop's two-piece set: 1.149 Kč against a "was" price of 5.745 Kč, "80% SLEVA".
    expect: "80% off: discounts this deep",
    page: {
      locale: "cs-CZ",
      height: 1300,
      blur: ["figure.logo__img"],
      marks: {
        was: { selector: "s.product__price--strike", text: "5.745,00" },
        now: { selector: "span.product__price--sale", text: "1.149,00" },
      },
    },
    popup: { finding: "80% off: discounts this deep" },
    script: {
      duration: 23,
      camera: [
        { t: 0.0, left: 0, top: 0, zoom: 1 },
        { t: 1.0, left: 0, top: 0, zoom: 1 },
        { t: 3.0, left: 0, top: 520, zoom: 1 },
        { t: 4.6, left: 0, top: 520, zoom: 1 },
        { t: 5.8, left: 4, top: 700, zoom: 1.45 },
        { t: 13.2, left: 4, top: 700, zoom: 1.45 },
      ],
      strokes: [
        { mark: "was", kind: "circle", at: 7.4, draw: 0.6 },
        { mark: "now", kind: "underline", at: 9.4, draw: 0.5 },
      ],
      captions: [
        { from: 0.4, to: 4.5, y: 1240, size: 78, text: "A two-piece set, 80% off." },
        { from: 4.7, to: 7.8, y: 1240, size: 78, text: "Look at the “was” price." },
        { from: 8.0, to: 13.2, y: 1130, size: 100, text: "Ever 5.745 Kč?" },
        { from: 9.7, to: 13.2, y: 1290, size: 74, text: "Or just a big number?", tone: "caution" },
        { from: 11.1, to: 13.2, y: 420, size: 54, text: "A big “was” makes any price look cheap.", tone: "soft" },
      ],
      check: {
        at: 13.2,
        findingY: 860,
        mark: 14.6,
        captions: [
          { from: 13.5, to: 19.8, y: 300, size: 66, text: "Unfret checks the same page" },
          { from: 15.4, to: 19.8, y: 1560, size: 60, text: "80% off a price nobody paid", tone: "caution" },
        ],
      },
      end: { at: 19.8 },
      mascot: mascotTrack({ enter: 5.0, spot: [150, 1662], look: [330, 1370], doubleTake: 8.1, nods: [9.95],
        blinks: [11.7], check: { spot: [870, 1468], lens: [720, 1250] } }),
    },
  },
  {
    name: "j-since-1989",
    // A Dutch bag shop that says "sinds 1989" (since 1989); its web address is months old.
    expect: "but its web address has existed for only",
    page: {
      locale: "nl-NL",
      height: 1500,
      blur: ["figure.logo__img"],
      marks: {
        since: { selector: "body", text: "1989" },
      },
    },
    popup: { finding: "but its web address has existed for only" },
    script: {
      duration: 23,
      camera: [
        { t: 0.0, left: 0, top: 0, zoom: 1 },
        { t: 1.0, left: 0, top: 0, zoom: 1 },
        { t: 3.0, left: 0, top: 700, zoom: 1 },
        { t: 4.6, left: 0, top: 700, zoom: 1 },
        { t: 5.8, left: 8, top: 950, zoom: 1.5 },
        { t: 13.2, left: 8, top: 950, zoom: 1.5 },
      ],
      strokes: [
        { mark: "since", kind: "circle", at: 7.4, draw: 0.6 },
      ],
      captions: [
        { from: 0.4, to: 4.5, y: 1240, size: 78, text: "A family shop, it says." },
        { from: 4.7, to: 7.8, y: 1240, size: 78, text: "Read the small print." },
        { from: 8.0, to: 13.2, y: 1130, size: 100, text: "Since 1989?" },
        { from: 9.7, to: 13.2, y: 1290, size: 74, text: "How old is the website?", tone: "caution" },
      ],
      check: {
        at: 13.2,
        findingY: 860,
        mark: 14.6,
        captions: [
          { from: 13.5, to: 19.8, y: 300, size: 66, text: "Unfret checks the same page" },
          { from: 15.4, to: 19.8, y: 1560, size: 60, text: "Its web address is 7 months old", tone: "caution" },
        ],
      },
      end: { at: 19.8 },
      mascot: mascotTrack({ enter: 5.0, spot: [150, 1662], look: [330, 1370], doubleTake: 8.1, nods: [9.95],
        blinks: [11.7], check: { spot: [870, 1468], lens: [720, 1250] } }),
    },
  },
  {
    name: "g-imported-reviews",
    // A Czech shop's memory journal with 1,035 star ratings, shown by Loox, a review app that imports reviews from
    // AliExpress.
    expect: "copies reviews from other sites",
    page: {
      locale: "cs-CZ",
      height: 1400,
      hide: ["div.sticky-add-to-cart__bar"],
      blur: ["img.header-logo__image"],
      marks: {
        stars: { selector: "span.loox-rating-label", text: "(1,035)" },
      },
    },
    popup: { finding: "copies reviews from other sites" },
    script: {
      duration: 23,
      camera: [
        { t: 0.0, left: 0, top: 0, zoom: 1 },
        { t: 1.0, left: 0, top: 0, zoom: 1 },
        { t: 3.0, left: 0, top: 420, zoom: 1 },
        { t: 4.6, left: 0, top: 420, zoom: 1 },
        { t: 5.8, left: 8, top: 700, zoom: 1.6 },
        { t: 13.2, left: 8, top: 700, zoom: 1.6 },
      ],
      strokes: [
        { mark: "stars", kind: "circle", at: 7.4, draw: 0.6 },
      ],
      captions: [
        { from: 0.4, to: 4.5, y: 1240, size: 78, text: "A journal for your mum." },
        { from: 4.7, to: 7.8, y: 1240, size: 78, text: "1,035 five-star ratings." },
        { from: 8.0, to: 13.2, y: 1130, size: 100, text: "Who wrote them?" },
        { from: 9.7, to: 13.2, y: 1290, size: 74, text: "Buyers of this journal?", tone: "caution" },
      ],
      check: {
        at: 13.2,
        findingY: 860,
        mark: 14.6,
        captions: [
          { from: 13.5, to: 19.8, y: 300, size: 66, text: "Unfret checks the same page" },
          { from: 15.4, to: 19.8, y: 1560, size: 60, text: "Reviews copied in by an app", tone: "caution" },
        ],
      },
      end: { at: 19.8 },
      mascot: mascotTrack({ enter: 5.0, spot: [150, 1662], look: [330, 1370], doubleTake: 8.1, nods: [9.95],
        blinks: [11.7], check: { spot: [870, 1417], lens: [720, 1195] } }),
    },
  },
  // The test shop (/test-shop on dev, "every trick here is copied from real listings, names removed"): one real deep
  // check of its product page (capture.mjs) is the result in each of these videos; each shows a different finding.
  // Kept for the record (their videos are in media/videos/test-shop); the published set is on real pages.
  {
    name: "ts-b-countdown",
    url: TEST_SHOP,
    expect: "This countdown restarts when you reload the page",
    page: {
      height: 1700,
      hide: SHOP_POPS,
      marks: {
        clock: { selector: ".countdown-clock", style: true },
        timer: { selector: ".countdown-timer" },
      },
    },
    popup: { finding: "This countdown restarts when you reload the page" },
    script: {
      duration: 23,
      camera: [
        { t: 0.0, left: 0, top: 0, zoom: 1 },
        { t: 1.0, left: 0, top: 0, zoom: 1 },
        { t: 3.0, left: 0, top: 900, zoom: 1 },
        { t: 4.6, left: 0, top: 900, zoom: 1 },
        { t: 5.8, left: 8, top: 1040, zoom: 1.75 },
        { t: 13.2, left: 8, top: 1040, zoom: 1.75 },
      ],
      // The page's own countdown, live (it starts at 15:00 on load), and a reload that starts it over.
      clocks: [{ mark: "clock", start: 900 }],
      reloads: [{ at: 8.0 }],
      strokes: [{ mark: "clock", kind: "circle", at: 8.7, draw: 0.6 }],
      captions: [
        { from: 0.4, to: 4.5, y: 1240, size: 78, text: "“Sale ends in 15 minutes.”" },
        { from: 4.7, to: 7.9, y: 1240, size: 78, text: "Now reload the page." },
        { from: 8.9, to: 13.2, y: 1130, size: 100, text: "Back to 15:00." },
        { from: 10.2, to: 13.2, y: 1290, size: 74, text: "It restarts on every visit", tone: "caution" },
        { from: 11.4, to: 13.2, y: 420, size: 54, text: "The deadline isn't real.", tone: "soft" },
      ],
      check: {
        at: 13.2,
        findingY: 860,
        mark: 14.6,
        captions: [
          { from: 13.5, to: 19.8, y: 300, size: 66, text: "Unfret checks the same page" },
          { from: 15.4, to: 19.8, y: 1560, size: 60, text: "It catches the restarting clock", tone: "caution" },
        ],
      },
      end: { at: 19.8 },
      mascot: mascotTrack({ enter: 5.0, spot: [150, 1662], look: [330, 1370], doubleTake: 9.0, nods: [10.45],
        blinks: [11.9], check: { spot: [870, 1417], lens: [720, 1195] } }),
    },
  },
  {
    name: "ts-f-was-price",
    url: TEST_SHOP,
    expect: "79% off: discounts this deep",
    page: {
      height: 1700,
      hide: SHOP_POPS,
      marks: {
        was: { selector: ".price-was", text: "$189.99" },
        save: { selector: ".price-save", text: "(79%)" },
        now: { selector: ".price-now", text: "$39.99" },
      },
    },
    popup: { finding: "79% off: discounts this deep" },
    script: {
      duration: 23,
      camera: [
        { t: 0.0, left: 0, top: 0, zoom: 1 },
        { t: 1.0, left: 0, top: 0, zoom: 1 },
        { t: 3.0, left: 0, top: 860, zoom: 1 },
        { t: 4.6, left: 0, top: 860, zoom: 1 },
        { t: 5.8, left: 4, top: 990, zoom: 1.35 },
        { t: 13.2, left: 4, top: 990, zoom: 1.35 },
      ],
      strokes: [
        { mark: "was", kind: "circle", at: 7.4, draw: 0.6 },
        { mark: "save", kind: "underline", at: 9.4, draw: 0.5 },
      ],
      captions: [
        { from: 0.4, to: 4.5, y: 1240, size: 78, text: "A star projector, 79% off." },
        { from: 4.7, to: 7.8, y: 1240, size: 78, text: "Look at the “was” price." },
        { from: 8.0, to: 13.2, y: 1130, size: 100, text: "Ever $189.99?" },
        { from: 9.7, to: 13.2, y: 1290, size: 74, text: "Nobody paid that price", tone: "caution" },
        { from: 11.1, to: 13.2, y: 420, size: 54, text: "A big “was” makes any price look cheap.", tone: "soft" },
      ],
      check: {
        at: 13.2,
        findingY: 860,
        mark: 14.6,
        captions: [
          { from: 13.5, to: 19.8, y: 300, size: 66, text: "Unfret checks the same page" },
          { from: 15.4, to: 19.8, y: 1560, size: 60, text: "It calls out the made-up discount", tone: "caution" },
        ],
      },
      end: { at: 19.8 },
      mascot: mascotTrack({ enter: 5.0, spot: [150, 1662], look: [330, 1370], doubleTake: 8.1, nods: [9.95],
        blinks: [11.7], check: { spot: [870, 1468], lens: [720, 1250] } }),
    },
  },
  {
    name: "ts-g-imported-reviews",
    url: TEST_SHOP,
    expect: "Loox (reviews with AliExpress import)",
    page: {
      height: 3900,
      hide: SHOP_POPS,
      marks: {
        first: { selector: "#looxReviews article:nth-of-type(1)", text: "Absolutely love it! My kids fall asleep so fast now." },
        again: { selector: "#looxReviews article:nth-of-type(7)", text: "Absolutely love it! My kids fall asleep so fast now." },
        shoes: { selector: "#looxReviews article:nth-of-type(5)", text: "These shoes fit perfectly" },
      },
    },
    // The finding is among the signals behind the verdict: the popup's own "Show details", opened.
    popup: { finding: "Loox (reviews with AliExpress import)", expand: true },
    script: {
      duration: 23,
      camera: [
        { t: 0.0, left: 0, top: 0, zoom: 1 },
        { t: 1.0, left: 0, top: 0, zoom: 1 },
        { t: 3.2, left: 0, top: 2480, zoom: 1 },
        { t: 5.0, left: 0, top: 2480, zoom: 1 },
        { t: 6.4, left: 0, top: 2900, zoom: 1 },
        { t: 13.2, left: 0, top: 2900, zoom: 1 },
      ],
      strokes: [
        { mark: "first", kind: "underline", at: 4.0, draw: 0.6 },
        { mark: "shoes", kind: "circle", at: 7.6, draw: 0.6 },
        { mark: "again", kind: "underline", at: 9.6, draw: 0.6 },
      ],
      captions: [
        { from: 0.4, to: 3.0, y: 1240, size: 78, text: "4.9 stars from 2,847 reviews." },
        { from: 3.4, to: 6.2, y: 1560, size: 70, text: "Read a few." },
        { from: 8.0, to: 13.2, y: 330, size: 84, text: "Shoes? On a projector?" },
        { from: 10.0, to: 13.2, y: 420, size: 66, text: "Same words, another name", tone: "caution" },
      ],
      check: {
        at: 13.2,
        findingY: 860,
        mark: 14.6,
        captions: [
          { from: 13.5, to: 19.8, y: 300, size: 66, text: "Unfret checks the same page" },
          { from: 15.4, to: 19.8, y: 1560, size: 60, text: "It finds reviews imported in bulk", tone: "caution" },
        ],
      },
      end: { at: 19.8 },
      // Not a close-up (the reviews at full size): it stays C, at the lower right, looking up the list.
      mascot: mascotTrack({ enter: 5.6, spot: [920, 1662], look: [700, 1370], doubleTake: 8.1, nods: [10.2],
        blinks: [11.9], turn: false, check: { spot: [870, 1261], lens: [720, 1040] } }),
    },
  },
  {
    name: "ts-h-same-photo",
    url: TEST_SHOP,
    expect: "sold elsewhere for $17.99",
    page: {
      height: 1300,
      hide: SHOP_POPS,
      marks: {
        photo: { selector: "#gallery-main" },
        brand: { selector: ".vendor", text: "LUMIQZEN" },
        now: { selector: ".price-now", text: "$39.99" },
      },
    },
    popup: { finding: "Looks like the same product, sold elsewhere for $17.99" },
    script: {
      duration: 23,
      camera: [
        { t: 0.0, left: 0, top: 0, zoom: 1 },
        { t: 1.2, left: 0, top: 0, zoom: 1 },
        { t: 3.0, left: 0, top: 150, zoom: 1 },
        { t: 7.0, left: 0, top: 150, zoom: 1 },
        { t: 8.6, left: 0, top: 700, zoom: 1 },
        { t: 13.2, left: 0, top: 700, zoom: 1 },
      ],
      strokes: [
        { mark: "brand", kind: "underline", at: 9.2, draw: 0.5 },
        { mark: "now", kind: "circle", at: 10.4, draw: 0.6 },
      ],
      captions: [
        { from: 0.4, to: 2.9, y: 1240, size: 78, text: "A LUMIQZEN projector." },
        { from: 3.2, to: 7.0, y: 1500, size: 78, text: "Their own design, right?" },
        { from: 9.0, to: 13.2, y: 1500, size: 84, text: "$39.99 here.", tone: "caution" },
        { from: 10.6, to: 13.2, y: 1620, size: 54, text: "Is the same photo anywhere else?", tone: "soft" },
      ],
      check: {
        at: 13.2,
        findingY: 640,
        mark: 14.6,
        captions: [
          { from: 13.5, to: 19.8, y: 300, size: 66, text: "Unfret checks the same page" },
          { from: 15.4, to: 19.8, y: 1560, size: 60, text: "Same photo, other brand, $17.99", tone: "caution" },
        ],
      },
      end: { at: 19.8 },
      // The check's finding fills the card, so the mascot only comes on for the end: it hops in and becomes the logo.
      mascot: {
        scale: 1.65,
        keys: [
          { t: 0, x: 1300, y: 815, pose: 0, look: [-3, 0], hand: "rest" },
          { t: 19.75, x: 1300, y: 815 },
          { t: 20.25, ease: "linear", x: 900, y: 815 },
        ],
        hops: [{ at: 19.75, dur: 0.5, height: 50 }],
        become: { at: 20.0, dur: 1.05, logo: "#end img", then: "#end .name, #end .line, #end .web, #end .free", arc: 30 },
      },
    },
  },
  {
    name: "ts-i-preticked-extra",
    url: TEST_SHOP,
    expect: "A paid extra is ticked for you in advance",
    page: {
      height: 1700,
      hide: SHOP_POPS,
      marks: {
        addon: { selector: "label.addon", text: "Add Worry-Free Delivery Protection (+$4.99)" },
        box: { selector: "label.addon input" },
        cart: { selector: ".btn-cart" },
      },
    },
    popup: { finding: "A paid extra is ticked for you in advance" },
    script: {
      duration: 23,
      camera: [
        { t: 0.0, left: 0, top: 0, zoom: 1 },
        { t: 1.0, left: 0, top: 0, zoom: 1 },
        { t: 3.2, left: 0, top: 1000, zoom: 1 },
        { t: 4.6, left: 0, top: 1000, zoom: 1 },
        { t: 5.8, left: 8, top: 1280, zoom: 1.75 },
        { t: 13.2, left: 8, top: 1280, zoom: 1.75 },
      ],
      strokes: [
        { mark: "box", kind: "circle", at: 7.6, draw: 0.6 },
        { mark: "addon", kind: "underline", at: 9.4, draw: 0.6 },
      ],
      captions: [
        { from: 0.4, to: 4.5, y: 1240, size: 78, text: "Pick a colour, add to cart." },
        { from: 4.7, to: 7.6, y: 1240, size: 78, text: "Look above the button." },
        { from: 8.0, to: 13.2, y: 1130, size: 100, text: "Already ticked." },
        { from: 9.7, to: 13.2, y: 1290, size: 74, text: "+$4.99 you didn't pick", tone: "caution" },
        { from: 11.1, to: 13.2, y: 420, size: 54, text: "Not allowed in the EU.", tone: "soft" },
      ],
      check: {
        at: 13.2,
        findingY: 860,
        mark: 14.6,
        captions: [
          { from: 13.5, to: 19.8, y: 300, size: 66, text: "Unfret checks the same page" },
          { from: 15.4, to: 19.8, y: 1560, size: 60, text: "It flags the extra ticked for you", tone: "caution" },
        ],
      },
      end: { at: 19.8 },
      mascot: mascotTrack({ enter: 5.0, spot: [150, 1662], look: [330, 1370], doubleTake: 8.1, nods: [9.95],
        blinks: [11.7], check: { spot: [870, 1498], lens: [708, 1306] } }),
    },
  }
];

/** The videos, each with its page and its brand's words to blur from pages.json. */
export const VIDEOS = VIDEO_LIST.map((video) => {
  const page = PAGES[video.name] ?? {};
  const brand = page.brand ?? [];
  return {
    ...video,
    url: video.url ?? page.url,
    page: { ...video.page, blurText: [...(video.page.blurText ?? []), ...brand] },
    popup: { ...video.popup, blurText: [...(video.popup.blurText ?? []), ...brand] },
  };
});
