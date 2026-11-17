// How a result looks, shared by the link checker (/check), shared results (/r/{id}) and the showcase pages
// (/showcase/{id}): the verdict band as a word and a sign, the headline, and the findings as cards.
import { ownThumbnails } from "./check-core.js";

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function listOf(items) {
  const ul = el("ul", "cards");
  ul.append(...items);
  return ul;
}

// A reason or a good point: its title, and what it means right below it (as the extension shows it: nothing to tap
// open). Photos matched on other shops fold out, since they're a closer look rather than the point.
export function card(className, title, detail, thumbs = []) {
  const li = el("li", `card ${className}`);
  li.append(el("span", "title", title));
  if (detail) li.append(el("p", "detail", detail));
  if (thumbs.length) {
    const photos = el("details");
    photos.append(el("summary", undefined, thumbs.length === 1 ? "See the matching photo" : "See the matching photos"));
    const row = el("div", "thumbs");
    for (const src of thumbs) {
      const img = el("img");
      img.src = src;
      img.alt = "A matching photo on another shop";
      img.width = 64;
      img.height = 64;
      img.loading = "lazy";
      img.referrerPolicy = "no-referrer";
      img.addEventListener("error", () => img.remove());
      row.append(img);
    }
    photos.append(row);
    li.append(photos);
  }
  return li;
}

/**
 * The headline without the label in front: results checked before headlines stopped repeating the label (cached,
 * shared or shown) still read "Many warning signs: fake countdown.", right under "Many warning signs" (the label
 * before the four verdicts got their current names).
 */
export function headlineOf(headline, label) {
  const text = String(headline ?? "");
  if (!label || !text.toLowerCase().startsWith(label.toLowerCase())) return text;
  const rest = text.slice(label.length).replace(/^\s*[:,.;!—–-]\s*(but\s+)?/i, "");
  return rest.split(/\s+/).length >= 2 ? rest[0].toUpperCase() + rest.slice(1) : text;
}

// Every verdict below "Looks fine" names at least one reason. The server always sends one (the catalogue's "Several
// small signals" when nothing else fired); a result without any (from before verdicts had reasons) gets the same words.
const OVERALL_REASON = {
  severity: "low",
  title: "Several small signals",
  detail: "No single big problem, but several smaller signals together lowered the score.",
};

/** The reasons to show: the verdict's, or OVERALL_REASON for a band below "Looks fine" without any. */
export function reasonsOf(result, band) {
  const reasons = result.verdict?.reasons ?? [];
  return reasons.length || !band || band === "reliable" ? reasons : [OVERALL_REASON];
}

/**
 * The verdict and its findings as nodes: `band` is the CSS band (null: not assessed), `label` and `symbol` the band
 * in words and as a sign (drawn by the stylesheet). The scorer's details (signals, model, confidence) stay in the
 * API for those who want them; a shopper gets the reasons, each with what it means.
 */
export function resultNodes(result, { band, label, symbol }) {
  const verdict = result.verdict;
  const head = el("div", `verdict ${band ?? "none"}`);
  const sign = el("span", "symbol", symbol);
  sign.setAttribute("aria-hidden", "true");
  head.append(sign, el("p", "label", label));
  const nodes = [head, el("p", "headline", headlineOf(verdict?.headline ?? result.explanation, label))];

  const shown = reasonsOf(result, band);
  const reasons = shown.length
    ? [
        el("h2", undefined, "What we noticed"),
        listOf(shown.map((r) => card(r.severity, r.title, r.detail, ownThumbnails(r.thumbnails, location.origin)))),
      ]
    : [];
  const positives = verdict?.positives?.length
    ? [el("h2", undefined, "What looks good"), listOf(verdict.positives.map((p) => card("good", p.title, p.detail)))]
    : [];
  nodes.push(...(band === "reliable" ? [...positives, ...reasons] : [...reasons, ...positives]));
  return { nodes };
}
