// "Spotted recently" on the home page and the link checker: the latest published showcase cases, real checks of
// real product pages that Unfret ran itself. The section stays hidden when there are none (or the list can't load).
import { el } from "./check-render.js";

const LIMIT = 3;

function caseItem(item) {
  const li = el("li", "card spotted-item");
  const head = el("div", `spotted-band ${item.band}`);
  const sign = el("span", "symbol", item.symbol);
  sign.setAttribute("aria-hidden", "true");
  head.append(sign, el("span", "spotted-label", item.label));
  const link = el("a", "spotted-title", item.title);
  link.href = item.page_url;
  li.append(head, link, el("p", "muted small", `${item.host} · checked on ${item.checked_on}`));
  return li;
}

async function show(section) {
  const response = await fetch(`/v1/showcase?limit=${LIMIT}`, { headers: { Accept: "application/json" } });
  if (!response.ok) return;
  const { items } = await response.json();
  if (!Array.isArray(items) || !items.length) return;
  section.querySelector(".spotted-list").replaceChildren(...items.slice(0, LIMIT).map(caseItem));
  section.hidden = false;
}

const section = document.getElementById("spotted");
if (section) show(section).catch(() => {});
