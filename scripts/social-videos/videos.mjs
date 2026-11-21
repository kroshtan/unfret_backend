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
