// An affiliate's own numbers. The secret token is in the link's fragment (/affiliates/stats#ufa_…), which a browser
// never sends to a server; this page sends it once, in a header, to GET /v1/affiliates/stats.

const $ = (id) => document.getElementById(id);
const TOKEN_RE = /^ufa_[a-z2-7]{52}$/;

function money(minor, currency) {
  const sign = minor < 0 ? "-" : "";
  const abs = Math.abs(minor);
  const symbol = currency === "eur" ? "€" : "$";
  return `${sign}${symbol}${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, "0")}`;
}

function amounts(byCurrency, field) {
  const parts = Object.entries(byCurrency ?? {})
    .filter(([, m]) => (field ? m[field] : m))
    .map(([currency, m]) => money(field ? m[field] : m, currency));
  return parts.join(" + ") || "–";
}

function cell(text) {
  const td = document.createElement("td");
  td.textContent = text;
  return td;
}

export async function load(win = window) {
  const token = win.location.hash.slice(1);
  const status = $("stats-status");
  if (!TOKEN_RE.test(token)) {
    status.textContent = "This page needs the private link we sent you. Lost it? Write to hello@unfret.net.";
    return;
  }
  const response = await fetch("/v1/affiliates/stats", { headers: { "X-Unfret-Stats": token }, cache: "no-store" });
  if (!response.ok) {
    status.textContent =
      response.status === 404
        ? "This link doesn't work any more: it was replaced. Write to hello@unfret.net for the new one."
        : "Your numbers can't be shown right now. Please try again later.";
    return;
  }
  const body = await response.json();
  $("stats-title").textContent = `Your affiliate numbers, ${body.name}`;
  const balance = Object.entries(body.balance ?? {});
  const total = (field) =>
    balance
      .filter(([, b]) => b[field])
      .map(([currency, b]) => money(b[field], currency))
      .join(" + ");
  $("stats-due").textContent = total("due") || "$0.00";
  const notes = [];
  if (total("held")) notes.push(`In the ${body.rules.hold_days}-day hold: ${total("held")}.`);
  if (total("carried_over")) notes.push(`Carried over to a later payout: ${total("carried_over")}.`);
  $("stats-balance").textContent = notes.join(" ");
  $("stats-hold").textContent = String(body.rules.hold_days);
  const min = body.rules.min_payout_cents;
  $("stats-min").textContent = `${money(min, "usd")} or ${money(min, "eur")}`;
  $("stats-codes").textContent = body.codes.join(", ");
  $("stats-link").textContent = body.codes.length ? `${win.location.origin}/?ref=${body.codes[0]}` : "";
  const rows = body.months.map((month) => {
    const tr = document.createElement("tr");
    tr.append(
      cell(month.month),
      cell(String(month.passes)),
      cell(String(month.first_checks)),
      cell(String(month.purchases)),
      cell(amounts(month.money, "net")),
      cell(amounts(month.money, "commission")),
      cell(amounts(month.money, "reversed")),
      cell(amounts(month.money, "paid")),
    );
    return tr;
  });
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = cell("Nothing yet: share your link, and the first passes show up here.");
    td.colSpan = 8;
    tr.append(td);
    rows.push(tr);
  }
  $("stats-rows").replaceChildren(...rows);
  status.textContent = body.active ? "" : "Your affiliate code is switched off: new passes don't count for you.";
  $("stats").hidden = false;
}

if (typeof window !== "undefined") void load();
