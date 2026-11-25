// The home page: the paste-a-link field (start-check.js), the install button for the visitor's browser, and the
// contributors' leaderboard (GET /v1/contributors/leaderboard: public names only). Without JavaScript the page still
// reads well: the leaderboard just says nobody is on it yet.
import { chooseButtons } from "./install-button.js";
import { wireStartForm } from "./start-check.js";

const LEADERBOARD_SIZE = 10;

/** The leaderboard's rows as plain data: rank, name and how many reports held up; junk rows left out. */
export function leaderboardRows(body, size = LEADERBOARD_SIZE) {
  const entries = Array.isArray(body?.entries) ? body.entries : [];
  return entries
    .filter((e) => typeof e?.handle === "string" && e.handle.trim() && Number.isFinite(Number(e.agreed)))
    .slice(0, size)
    .map((e, i) => ({ rank: Number(e.rank) || i + 1, handle: e.handle.trim(), agreed: Number(e.agreed) }));
}

async function leaderboard() {
  const list = document.getElementById("leaderboard");
  if (!list) return;
  const response = await fetch(`/v1/contributors/leaderboard?limit=${LEADERBOARD_SIZE}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) return;
  const rows = leaderboardRows(await response.json());
  if (!rows.length) return;
  list.replaceChildren(
    ...rows.map((row) => {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.className = "who";
      name.textContent = row.handle;
      const count = document.createElement("span");
      count.className = "count";
      count.textContent = row.agreed === 1 ? "1 confirmed report" : `${row.agreed} confirmed reports`;
      li.value = row.rank;
      li.append(name, count);
      return li;
    }),
  );
  list.hidden = false;
  document.getElementById("leaderboard-empty").hidden = true;
}

const form = typeof document === "undefined" ? null : document.getElementById("start-check");
if (form) {
  wireStartForm(form, { empty: form.dataset.empty ?? "", invalid: form.dataset.invalid ?? "" });
  chooseButtons();
  leaderboard().catch(() => {});
}
