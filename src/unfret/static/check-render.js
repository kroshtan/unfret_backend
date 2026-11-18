// How a result looks, shared by the link checker (/check) and shared results (/r/{id}): the result card. The verdict
// band as a word and a sign and the headline; the same product sold elsewhere, first, with both photos; the findings
// as cards, each with where its evidence comes from; one suggested next step; and what was checked. The extension
// and the Android app show the same card.
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

// A reason or a good point: its title, what it means right below it (as the extension shows it: nothing to tap
// open) and where the evidence comes from. Photos matched on other shops fold out, since they're a closer look rather
// than the point.
export function card(className, title, detail, thumbs = [], source = "") {
  const li = el("li", `card ${className}`);
  li.append(el("span", "title", title));
  if (detail) li.append(el("p", "detail", detail));
  if (source) li.append(el("p", "source", `Source: ${source}`));
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

// The checklist's marks, as words for screen readers (the marks themselves are drawn by the stylesheet).
const CHECK_WORD = { checked: "checked", concern: "something to note", not_checked: "not checked" };

/**
 * The result card as plain data (no DOM), for `resultNodes` and the tests: what comes first, the findings in order,
 * the next step and the checklist. Older results (cached, shared before the card) have no checklist, step or same
 * product: those parts are simply left out.
 */
export function cardOf(result, band, origin) {
  const verdict = result.verdict ?? {};
  const reasons = reasonsOf(result, band);
  const positives = verdict.positives ?? [];
  const same = verdict.same_product;
  const image = same ? ownThumbnails([same.image], origin)[0] : undefined;
  const sameProduct = same && image
    ? {
        title: same.title,
        detail: same.detail,
        where: same.where,
        image,
        ownImage: ownThumbnails([same.own_image], origin)[0],
      }
    : null;
  // The photo of the same product is shown once, up top: not again under "Same photo on other shops".
  const thumbsOf = (r) => (sameProduct && r.type === "photo_reused" ? [] : ownThumbnails(r.thumbnails, origin));
  const checks = typeof verdict.checks_run === "number" ? verdict.checks_run : null;
  return {
    sameProduct,
    reasons: reasons.map((r) => ({ ...r, thumbs: thumbsOf(r) })),
    positives,
    positivesFirst: band === "reliable",
    clean: band === "reliable" && reasons.length === 0 && checks ? `Nothing worrying in ${checks} checks.` : null,
    nextStep: verdict.next_step?.text ?? null,
    checklist: (verdict.checklist ?? []).map((item) => ({
      label: item.label,
      status: item.status in CHECK_WORD ? item.status : "checked",
      note: item.note ?? "",
    })),
    checks,
  };
}

function sameProductNode(same) {
  const box = el("section", "same-product");
  box.setAttribute("aria-label", same.title);
  box.append(el("p", "title", same.title));
  const photos = el("div", "pair");
  for (const [src, caption] of [
    [same.ownImage, "This page"],
    [same.image, same.where],
  ]) {
    if (!src) continue;
    const figure = el("figure");
    const img = el("img");
    img.src = src;
    img.alt = caption === "This page" ? "This page's product photo" : `The same photo on ${caption}`;
    img.width = 120;
    img.height = 120;
    img.referrerPolicy = "no-referrer";
    img.addEventListener("error", () => figure.remove());
    figure.append(img, el("figcaption", undefined, caption));
    photos.append(figure);
  }
  box.append(photos, el("p", "detail", same.detail));
  return box;
}

function checklistNode(items) {
  const ul = el("ul", "checked-list");
  for (const item of items) {
    const li = el("li", item.status);
    const mark = el("span", "mark");
    mark.setAttribute("aria-hidden", "true");
    const text = el("span", "what");
    text.append(el("span", "area", item.label));
    const hidden = el("span", "visually-hidden", `: ${CHECK_WORD[item.status]}`);
    text.append(hidden);
    if (item.note) text.append(el("span", "why", item.note));
    li.append(mark, text);
    ul.append(li);
  }
  return ul;
}

/**
 * The verdict and its card as nodes: `band` is the CSS band (null: not assessed), `label` and `symbol` the band in
 * words and as a sign (drawn by the stylesheet). The scorer's details (signals, model, confidence) stay in the API for
 * those who want them; a shopper gets the findings, each with what it means and where it comes from.
 */
export function resultNodes(result, { band, label, symbol }) {
  const verdict = result.verdict;
  const head = el("div", `verdict ${band ?? "none"}`);
  const sign = el("span", "symbol", symbol);
  sign.setAttribute("aria-hidden", "true");
  head.append(sign, el("p", "label", label));
  const nodes = [head, el("p", "headline", headlineOf(verdict?.headline ?? result.explanation, label))];
  const view = cardOf(result, band, location.origin);

  if (view.sameProduct) nodes.push(sameProductNode(view.sameProduct));
  if (view.clean) nodes.push(el("p", "clean", view.clean));
  const reasons = view.reasons.length
    ? [
        el("h2", undefined, "What we noticed"),
        listOf(view.reasons.map((r) => card(r.severity, r.title, r.detail, r.thumbs, r.source))),
      ]
    : [];
  const positives = view.positives.length
    ? [el("h2", undefined, "What looks good"), listOf(view.positives.map((p) => card("good", p.title, p.detail, [], p.source)))]
    : [];
  nodes.push(...(view.positivesFirst ? [...positives, ...reasons] : [...reasons, ...positives]));
  if (view.nextStep) {
    const step = el("section", "next-step");
    step.append(el("h2", undefined, "What you can do"), el("p", undefined, view.nextStep));
    nodes.push(step);
  }
  if (view.checklist.length) {
    const heading = el("h2", undefined, "What we checked");
    if (view.checks) heading.append(el("span", "count", ` · ${view.checks} checks`));
    nodes.push(heading, checklistNode(view.checklist));
  }
  return { nodes };
}

/**
 * A quick result's suggestion of a deep check, as plain data, or null: only when the server suggests one (an older
 * server, a deep result, a page that couldn't be assessed have none). The words are the server's.
 */
export function deepHintOf(result) {
  const offer = result?.verdict?.deep_check;
  if (result?.tier !== "quick" || !offer?.suggested || !offer.text) return null;
  const action = offer.action === "run_deep" ? "run_deep" : "get_checks";
  return {
    text: offer.text,
    cost: offer.cost ?? "",
    action,
    label: offer.action_label || (action === "run_deep" ? "Run a deep check" : "Get checks"),
    quiet: offer.quiet === true,
  };
}

/** The suggestion as a section with one real button (`run`) or a link to `checkoutHref`. */
export function deepHintNode(hint, { run, checkoutHref }) {
  const box = el("section", hint.quiet ? "deep-hint quiet" : "deep-hint");
  const text = el("p", undefined, hint.text);
  text.id = "deep-hint-text";
  box.setAttribute("aria-labelledby", text.id);
  box.append(text);
  if (hint.cost) box.append(el("p", "cost", hint.cost));
  const row = el("p", "buttons");
  if (hint.action === "run_deep") {
    const button = el("button", undefined, hint.label);
    button.type = "button";
    button.addEventListener("click", run);
    row.append(button);
  } else {
    const link = el("a", "button", hint.label);
    link.href = checkoutHref;
    row.append(link);
  }
  box.append(row);
  return box;
}
