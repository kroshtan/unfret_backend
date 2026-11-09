// How a result looks, shared by the link checker (/check) and the showcase pages (/showcase/{id}): the verdict band
// as a word and a symbol, the headline, the findings as cards with an expandable "Why?", and the details.
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

export function card(className, title, detail, thumbs = []) {
  const li = el("li", `card ${className}`);
  li.append(el("span", "title", title));
  if (detail || thumbs.length) {
    const why = el("details");
    why.append(el("summary", undefined, "Why?"));
    if (detail) why.append(el("p", undefined, detail));
    if (thumbs.length) {
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
      why.append(row);
    }
    li.append(why);
  }
  return li;
}

/**
 * The verdict and findings of a result, and its details (or null): `band` is the CSS band (null: not assessed),
 * `label` and `symbol` the band in words and as a sign.
 */
export function resultNodes(result, { band, label, symbol }) {
  const verdict = result.verdict;
  const head = el("div", `verdict ${band ?? "none"}`);
  const sign = el("span", "symbol", symbol);
  sign.setAttribute("aria-hidden", "true");
  head.append(sign, el("p", "label", label));
  const nodes = [head, el("p", "headline", verdict?.headline ?? result.explanation)];

  const reasons = verdict?.reasons?.length
    ? [
        el("h2", undefined, "What we noticed"),
        listOf(verdict.reasons.map((r) => card(r.severity, r.title, r.detail, ownThumbnails(r.thumbnails, location.origin)))),
      ]
    : [];
  const positives = verdict?.positives?.length
    ? [el("h2", undefined, "What looks good"), listOf(verdict.positives.map((p) => card("good", p.title, p.detail)))]
    : [];
  nodes.push(...(band === "reliable" ? [...positives, ...reasons] : [...reasons, ...positives]));
  return { nodes, details: detailsNode(result.assessment) };
}

function detailsNode(assessment) {
  if (!assessment?.flags?.length) return null;
  const details = el("details", "details");
  details.append(el("summary", undefined, "Show details"));
  const signals = assessment.flags.filter((f) => f.kind !== "context");
  const context = assessment.flags.filter((f) => f.kind === "context");
  for (const [title, flags] of [
    ["Signals", signals],
    ["About this page", context],
  ]) {
    if (!flags.length) continue;
    const ul = el("ul");
    for (const flag of flags) {
      const li = el("li", undefined, flag.label ?? "Signal");
      if (flag.evidence) li.append(document.createTextNode(`: ${flag.evidence}`));
      ul.append(li);
    }
    details.append(el("h3", undefined, title), ul);
  }
  const confidence = typeof assessment.confidence === "number" ? `${Math.round(assessment.confidence * 100)}%` : "–";
  details.append(el("p", "muted small", `Model ${assessment.model_version || "–"} · confidence ${confidence}`));
  return details;
}
