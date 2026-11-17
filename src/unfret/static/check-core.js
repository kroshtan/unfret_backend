// The link checker's pure parts (no DOM, no network), shared by check.js and its tests: finding the link in shared
// text, the verdict bands, the progress checklist and a server-sent events parser for fetch() streams.

// Apps share "Title <link>" or "Look at this: <link>!"; a bare "shop.example/item" is taken as an https link.
const LINK_RE = /https?:\/\/[^\s<>"'`]+/i;
const BARE_RE = /^(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[/?#]\S*)?$/i;

function trimTrailing(link) {
  let out = link.replace(/[.,;:!?'"»”’]+$/u, "");
  // A closing bracket belongs to the link only when the link opened one, e.g. a Wikipedia article.
  while (/[)\]}>]$/.test(out)) {
    const close = out.at(-1);
    const open = { ")": "(", "]": "[", "}": "{", ">": "<" }[close];
    if (out.split(open).length >= out.split(close).length) break;
    out = out.slice(0, -1).replace(/[.,;:!?'"»”’]+$/u, "");
  }
  return out;
}

/** The first http(s) link in some text, normalised, or "" when there is none. */
export function extractLink(text) {
  const value = String(text ?? "").trim();
  if (!value) return "";
  const match = LINK_RE.exec(value);
  let link = match ? trimTrailing(match[0]) : "";
  if (!link && BARE_RE.test(value)) link = `https://${value}`;
  if (!link) return "";
  try {
    const url = new URL(link);
    if ((url.protocol !== "http:" && url.protocol !== "https:") || !url.hostname.includes(".")) return "";
    return url.href;
  } catch {
    return "";
  }
}

/** The link a share or a deep link carries: `url`, else the first link in `text`, else in `title`. */
export function sharedLink(params) {
  for (const name of ["url", "text", "title"]) {
    const link = extractLink(params.get(name));
    if (link) return link;
  }
  return "";
}

// The same words and symbols as the extension, so a verdict never relies on colour alone.
export const BAND_WORD = {
  reliable: "Looks fine",
  concerns: "Worth a look",
  careful: "Think twice",
  junk_or_scam: "Serious concerns",
};
export const BAND_SYMBOL = { reliable: "✓", concerns: "!", careful: "⚠", junk_or_scam: "✕" };

/** The band for a result: the server's verdict, or the same thresholds from the score; null when not assessed. */
export function bandOf(result) {
  if (typeof result.score !== "number" || result.score < 0) return null;
  if (result.verdict?.band in BAND_WORD) return result.verdict.band;
  if (result.score >= 80) return "reliable";
  if (result.score >= 50) return "concerns";
  if (result.score >= 25) return "careful";
  return "junk_or_scam";
}

// A fixed, friendly checklist instead of skill names, one per kind of page: each item ticks off when one of its
// checks finishes. The classifier's step names the page type; until then, and for a page type not listed, the shop's.
export const CHECKLISTS = {
  shop: [
    { label: "Who's behind the shop", skills: ["eu_registry", "us_identity", "domain_age", "shop_policies"] },
    { label: "The product and the brand", skills: ["listing", "content", "brand_presence"] },
    { label: "Reviews", skills: ["reviews", "review_copies"] },
    { label: "Prices and discounts", skills: ["pricing", "market_price", "dark_patterns"] },
    { label: "Hidden tricks", skills: ["html_source_signals", "injection"] },
  ],
  ad: [
    { label: "The seller", skills: ["seller_profile"] },
    { label: "The price", skills: ["price_outlier"] },
    { label: "The photos", skills: ["reverse_image", "duplicate_ads"] },
    { label: "How you'd pay", skills: ["off_platform"] },
    { label: "The ad's text", skills: ["content", "injection"] },
  ],
  service: [
    { label: "Who the company is", skills: ["service_identity", "platform_profile", "eu_registry", "us_identity"] },
    { label: "Where they really are", skills: ["doorway_pages", "shop_links", "domain_age"] },
    { label: "Reviews", skills: ["testimonials"] },
    { label: "Prices and payment", skills: ["service_terms", "dark_patterns"] },
    { label: "The photos", skills: ["reverse_image"] },
  ],
};

/** Which checklist a page type gets: "shop", "ad" or "service". */
export function checklistFor(pageType) {
  if (pageType === "p2p_listing") return "ad";
  if (pageType === "service_listing" || pageType === "service_site") return "service";
  return "shop";
}

export function checklistDone(steps, finished, kind = "shop") {
  return CHECKLISTS[kind].map((item) => finished || item.skills.some((skill) => steps.includes(skill)));
}

/** Thumbnails may only come from this server's own image proxy, so the page never loads anything from a shop. */
export function ownThumbnails(urls, origin) {
  if (!Array.isArray(urls)) return [];
  return urls
    .filter((url) => {
      try {
        const parsed = new URL(url, origin);
        return parsed.origin === origin && parsed.pathname.startsWith("/v1/images/");
      } catch {
        return false;
      }
    })
    .slice(0, 3);
}

/** Incremental server-sent events parser: feed text, get the complete events ({event, data}) seen so far. */
export class SseParser {
  constructor() {
    this.buffer = "";
    this.name = "";
    this.data = [];
  }

  feed(chunk) {
    this.buffer += chunk;
    const events = [];
    for (;;) {
      const match = /\r\n|\r|\n/.exec(this.buffer);
      if (!match) break;
      // A trailing "\r" may be the first half of "\r\n": wait for more.
      if (match[0] === "\r" && match.index === this.buffer.length - 1) break;
      const line = this.buffer.slice(0, match.index);
      this.buffer = this.buffer.slice(match.index + match[0].length);
      const event = this.line(line);
      if (event) events.push(event);
    }
    return events;
  }

  end() {
    const events = this.buffer ? this.feed("\n") : [];
    const last = this.line("");
    return last ? [...events, last] : events;
  }

  line(line) {
    if (line === "") {
      if (this.data.length === 0) {
        this.name = "";
        return null;
      }
      const event = { event: this.name || "message", data: this.data.join("\n") };
      this.name = "";
      this.data = [];
      return event;
    }
    if (line.startsWith(":")) return null;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.name = value;
    else if (field === "data") this.data.push(value);
    return null;
  }
}
