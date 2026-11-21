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
      // hops in to the card's lower right (870, 1498) and holds the glass up to the finding, then jumps up beside the
      // logo on the end card (880, 815), grins and waves the glass.
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
          // The end card: jumps up beside the logo, grins, waves the glass.
          { t: 19.7, ease: "out", hand: "rest", mag: 0, reach: 0, lean: 0, look: [-3, 0] },
          { t: 19.85, x: 870, y: 1498 },
          { t: 20.45, ease: "linear", x: 880, y: 815 },
          { t: 20.9, look: [-5, 0], grin: 0 },
          { t: 21.6, ease: "spring", grin: 1, look: [-2, -1] },
        ],
        hops: [
          { at: 5.2, dur: 0.42, height: 60 },
          { at: 5.67, dur: 0.43, height: 50 },
          { at: 6.32, dur: 0.26, height: 16 },
          { at: 7.98, dur: 0.2, height: 14 },
          { at: 14.5, dur: 0.45, height: 60 },
          { at: 19.85, dur: 0.6, height: 120 },
        ],
        blinks: [6.9, 8.9, 11.7, 12.7, 16.6, 18.3, 22.4],
        nods: [{ at: 10.05 }, { at: 10.5 }, { at: 15.75, depth: 0.6 }],
        waves: [{ at: 21.7, dur: 1.0, amp: 14 }],
      },
    },
  },
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
