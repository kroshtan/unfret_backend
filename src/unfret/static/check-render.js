// How a result looks, shared by the link checker (/check) and shared results (/r/{id}): the result card. The risk
// level (0 none … 4 worst) as a number and words, its one-line explanation and the reasons behind it, worst first (an
// older result without a level keeps its verdict band as a word and a sign, and its headline); when part of the check didn't finish, one line that says so; the same
// product sold elsewhere, first, with both photos; the findings
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
export function card(className, title, detail, thumbs = [], source = "", links = []) {
  const li = el("li", `card ${className}`);
  li.append(el("span", "title", title));
  if (detail) li.append(el("p", "detail", detail));
  if (source) li.append(el("p", "source", `Source: ${source}`));
  const archived = archiveLinks(links);
  if (archived.length) {
    const row = el("p", "source", archived.length === 1 ? "The earlier copy: " : "The earlier copies: ");
    archived.forEach((href, i) => {
      const a = el("a", undefined, monthOf(href));
      a.href = href;
      a.rel = "noopener noreferrer nofollow";
      a.target = "_blank";
      row.append(...(i ? [", ", a] : [a]));
    });
    li.append(row);
  }
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

const WAYBACK_LINK = "https://web.archive.org/web/";
const COMMON_CRAWL_LINK = "https://index.commoncrawl.org/CC-MAIN-";

/**
 * A reason's links: archived copies of the shop's address only (web.archive.org, or the copy's record in Common
 * Crawl's index), at most three.
 */
export function archiveLinks(links) {
  if (!Array.isArray(links)) return [];
  const archived = (u) => u.startsWith(WAYBACK_LINK) || u.startsWith(COMMON_CRAWL_LINK);
  return links.filter((u) => typeof u === "string" && archived(u)).slice(0, 3);
}

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
  "November", "December"];

/** "March 2026" for an archived copy's link (its timestamp), else the archive's name. */
export function monthOf(href) {
  const m = /\/web\/(\d{4})(\d{2})|[?&]from=(\d{4})(\d{2})/.exec(href);
  const year = m && (m[1] || m[3]);
  const month = m && +(m[2] || m[4]);
  if (year && month >= 1 && month <= 12) return `${MONTHS[month - 1]} ${year}`;
  return href.startsWith(COMMON_CRAWL_LINK) ? "commoncrawl.org" : "web.archive.org";
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

// Every verdict below "Looks fine" names at least one reason. The server always sends one (its small signs, or "Nothing
// clear-cut" when nothing else fired); a result without any (from before verdicts had reasons) gets the same words.
const OVERALL_REASON = {
  severity: "low",
  title: "Nothing clear-cut",
  detail: "Nothing on the page stands out by itself, but taken together it scored lower than most.",
};

/** The reasons to show: the verdict's, or OVERALL_REASON for a band below "Looks fine" without any. */
export function reasonsOf(result, band) {
  const reasons = result.verdict?.reasons ?? [];
  return reasons.length || !band || band === "reliable" ? reasons : [OVERALL_REASON];
}

// The checklist's marks, as words for screen readers (the marks themselves are drawn by the stylesheet).
const CHECK_WORD = { checked: "nothing to note", concern: "something to note", not_checked: "not checked" };

/** "1 check", "16 checks". */
export function countOf(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

/**
 * The checklist's heading: "What we checked: 16 checks in 5 areas", the areas being those with a check (their counts
 * add up to the number). An older result without per-area counts keeps "What we checked · 16 checks".
 */
export function checklistHeading(checks, items) {
  if (!checks) return { title: "What we checked", count: "" };
  if (items.length && items.every((i) => typeof i.checks === "number")) {
    const areas = items.filter((i) => i.checks > 0).length;
    return { title: `What we checked: ${countOf(checks, "check", "checks")} in ${countOf(areas, "area", "areas")}`, count: "" };
  }
  return { title: "What we checked", count: ` · ${countOf(checks, "check", "checks")}` };
}

/**
 * The result card as plain data (no DOM), for `resultNodes` and the tests: what comes first, the findings in order,
 * the next step and the checklist, and the server's line when part of the check didn't finish (`incomplete`). Older
 * results (cached, shared before the card) have no checklist, step, same product or line: those parts are simply left
 * out.
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
  const checklist = (verdict.checklist ?? []).map((item) => ({
    label: item.label,
    status: item.status in CHECK_WORD ? item.status : "checked",
    note: item.note ?? "",
    // How many checks looked at this area (null on an older result).
    checks: typeof item.checks === "number" ? item.checks : null,
  }));
  return {
    incomplete: verdict.incomplete?.text || null,
    sameProduct,
    reasons: reasons.map((r) => ({ ...r, thumbs: thumbsOf(r) })),
    positives,
    positivesFirst: band === "reliable",
    clean: band === "reliable" && reasons.length === 0 && checks ? `Nothing worrying in ${checks} checks.` : null,
    nextStep: verdict.next_step?.text ?? null,
    checklist,
    checklistHeading: checklistHeading(checks, checklist),
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
    if (item.checks) text.append(el("span", "count", ` · ${countOf(item.checks, "check", "checks")}`));
    const hidden = el("span", "visually-hidden", `: ${CHECK_WORD[item.status]}`);
    text.append(hidden);
    if (item.note) text.append(el("span", "why", item.note));
    li.append(mark, text);
    ul.append(li);
  }
  return ul;
}

// A level's words when a result doesn't carry its own (the copy catalogue's, en.json "levels").
export const LEVEL_WORD = {
  4: "May not arrive, or may not be safe",
  3: "May not work as described",
  2: "May cost more than it's worth",
  1: "Pressure and tricks",
  0: "No concerns found",
};
// How many reasons behind a level show before "Show N more".
export const BECAUSE_SHOWN = 5;

/**
 * The result's risk level as plain data, or null for a result without one (older, shared before levels, not assessed):
 * `{ level, label, explanation, because: [{ type, level, title }], modifier }`, the reasons worst first.
 */
export function riskOf(result) {
  const risk = result?.verdict?.risk ?? result?.risk;
  const level = Number(risk?.level);
  if (!risk || !Number.isInteger(level) || level < 0 || level > 4) return null;
  const because = (Array.isArray(risk.because) ? risk.because : [])
    .filter((b) => b && typeof b.title === "string" && b.title)
    .map((b) => ({ type: String(b.type ?? ""), level: Number.isInteger(b.level) ? b.level : null, title: b.title }))
    .sort((a, b) => (b.level ?? -1) - (a.level ?? -1));
  return {
    level,
    label: typeof risk.label === "string" && risk.label ? risk.label : LEVEL_WORD[level],
    explanation: typeof risk.explanation === "string" ? risk.explanation : "",
    because,
    modifier: typeof risk.modifier?.text === "string" && risk.modifier.text ? risk.modifier.text : null,
  };
}

function becauseList(items) {
  const ul = el("ul", "because");
  for (const item of items) {
    const li = el("li");
    const num = el("span", "level-num", item.level === null ? "–" : String(item.level));
    if (item.level !== null) li.className = `level-${item.level}`;
    num.setAttribute("aria-hidden", "true");
    li.append(num, el("span", undefined, item.title));
    if (item.level !== null) li.append(el("span", "visually-hidden", ` (level ${item.level})`));
    ul.append(li);
  }
  return ul;
}

/** The level's head, explanation and reasons as nodes. */
export function riskNodes(risk) {
  const head = el("div", `verdict level-${risk.level}`);
  const num = el("span", "level-num", String(risk.level));
  num.setAttribute("aria-hidden", "true");
  const words = el("div");
  words.append(el("p", "scale", `Risk level ${risk.level} of 4`), el("p", "label", risk.label));
  if (risk.modifier) words.append(el("p", "modifier", risk.modifier));
  head.append(num, words);
  const nodes = [head];
  if (risk.explanation) nodes.push(el("p", "headline", risk.explanation));
  if (risk.because.length) {
    nodes.push(el("h2", undefined, "Why, worst first"), becauseList(risk.because.slice(0, BECAUSE_SHOWN)));
    const rest = risk.because.slice(BECAUSE_SHOWN);
    if (rest.length) {
      const more = el("details", "because-more");
      more.append(el("summary", undefined, `Show ${rest.length} more`), becauseList(rest));
      nodes.push(more);
    }
  }
  return nodes;
}

/**
 * The verdict and its card as nodes: the risk level when the result has one, else (an older result) `band` is the CSS band (null: not assessed), `label` and `symbol` the band in
 * words and as a sign (drawn by the stylesheet). The scorer's details (signals, model, confidence) stay in the API for
 * those who want them; a shopper gets the findings, each with what it means and where it comes from.
 */
export function resultNodes(result, { band, label, symbol }) {
  const verdict = result.verdict;
  const risk = riskOf(result);
  let nodes;
  if (risk) {
    nodes = riskNodes(risk);
  } else {
    const head = el("div", `verdict ${band ?? "none"}`);
    const sign = el("span", "symbol", symbol);
    sign.setAttribute("aria-hidden", "true");
    head.append(sign, el("p", "label", label));
    nodes = [head, el("p", "headline", headlineOf(verdict?.headline ?? result.explanation, label))];
  }
  // With a level, "nothing to note" is level 0: its good points come first.
  const view = cardOf(result, risk ? (risk.level === 0 ? "reliable" : "risk") : band, location.origin);

  // Right under the headline: why the verdict may be milder than it should be.
  if (view.incomplete) nodes.push(el("p", "incomplete", view.incomplete));
  if (view.sameProduct) nodes.push(sameProductNode(view.sameProduct));
  if (view.clean) nodes.push(el("p", "clean", view.clean));
  const reasons = view.reasons.length
    ? [
        el("h2", undefined, "What we noticed"),
        listOf(view.reasons.map((r) => card(r.severity, r.title, r.detail, r.thumbs, r.source, r.links))),
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
    const heading = el("h2", undefined, view.checklistHeading.title);
    if (view.checklistHeading.count) heading.append(el("span", "count", view.checklistHeading.count));
    nodes.push(heading, checklistNode(view.checklist));
  }
  return { nodes };
}

/**
 * What the check cost, as one sentence under the result: the server's (`charge_note`, with the checks left), or the
 * same words without the count from an older server's fields.
 */
export function chargeNoteOf(result) {
  if (result?.charge_note) return result.charge_note;
  if (result?.tier === "quick") return "Quick checks are free.";
  if (result?.score < 0) return "This page couldn't be assessed, so no check was used.";
  if (result?.charged === false) return "You checked this page recently, so no new check was used.";
  if (result?.free_check) return "This check used one of your free checks.";
  return result?.charged ? "This check used one of your checks." : null;
}

/**
 * A quick result's suggestion of a deep check, as plain data, or null: only when the server suggests one (an older
 * server, a deep result, a page that couldn't be assessed have none). The words are the server's.
 */
export function deepHintOf(result) {
  const offer = result?.verdict?.deep_check;
  if (result?.tier !== "quick" || !offer?.suggested || !offer.text) return null;
  const action = ["run_deep", "donate"].includes(offer.action) ? offer.action : offer.action ? "get_checks" : "none";
  return {
    text: offer.text,
    cost: offer.cost ?? "",
    action,
    label:
      offer.action_label ||
      (action === "run_deep" ? "Run a deep check" : action === "donate" ? "Support Unfret" : action === "none" ? "" : "Get checks"),
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
  if (hint.action === "none") return box;
  const row = el("p", "buttons");
  if (hint.action === "run_deep") {
    const button = el("button", undefined, hint.label);
    button.type = "button";
    button.addEventListener("click", run);
    row.append(button);
  } else {
    const link = el("a", "button", hint.label);
    link.href = hint.action === "donate" ? "/donate" : checkoutHref;
    row.append(link);
  }
  box.append(row);
  return box;
}

// ── "Wrong result?" (check.js): what a report may name, and the contributor key ─────────────────────────────

/** The findings a report can call wrong: those the result showed (its cards and the reasons behind its level). */
export function reportableFindings(result) {
  const seen = new Map();
  for (const item of [...(result?.verdict?.reasons ?? []), ...(result?.verdict?.risk?.because ?? [])]) {
    if (item?.type && item.title && item.type !== "overall" && !seen.has(item.type)) seen.set(item.type, item.title);
  }
  return [...seen].map(([type, title]) => ({ type, title }));
}

/** 32 random bytes in base64url without padding (43 characters), as /v1/labels expects. */
export function newContributorKey(bytes = crypto.getRandomValues(new Uint8Array(32))) {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
