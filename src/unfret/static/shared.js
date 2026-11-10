// A shared result's page (/r/{id}): the server puts the share's facts and verdict in the HTML and the result in a
// data block; this renders the result like the link checker does (the showcase's renderer), and wires the shop
// owner's objection and the sharer's delete. Nothing is stored in the browser.
import { resultNodes } from "./check-render.js";

const $ = (id) => document.getElementById(id);
const shareId = window.location.pathname.split("/").pop();

try {
  const result = JSON.parse($("case-data")?.textContent ?? "null");
  if (result?.verdict) {
    const { nodes, details } = resultNodes(result, {
      band: result.verdict.band,
      label: result.verdict.label,
      symbol: result.verdict.symbol,
    });
    if (details) nodes.push(details);
    $("result-body").replaceChildren(...nodes);
  }
} catch {
  // the server-rendered verdict stays
}

async function detailOf(response) {
  const body = await response.json().catch(() => ({}));
  return typeof body.detail === "string" ? body.detail : "";
}

$("object-send").addEventListener("click", async () => {
  const status = $("object-status");
  $("object-send").disabled = true;
  status.textContent = "Sending…";
  try {
    const response = await fetch(`/v1/shares/${encodeURIComponent(shareId)}/object`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: $("object-reason").value.trim() }),
    });
    if (response.ok) {
      status.textContent = "Thank you. It is hidden now, and we'll review it.";
      return;
    }
    status.textContent = (await detailOf(response)) || "That didn't work. Please try again later.";
  } catch {
    status.textContent = "Couldn't reach Unfret. Check your connection.";
  }
  $("object-send").disabled = false;
});

$("delete-send").addEventListener("click", async () => {
  const status = $("delete-status");
  const code = $("delete-code").value.trim();
  if (!code) {
    status.textContent = "Enter the delete code first.";
    return;
  }
  $("delete-send").disabled = true;
  status.textContent = "Deleting…";
  try {
    const response = await fetch(`/v1/shares/${encodeURIComponent(shareId)}`, {
      method: "DELETE",
      headers: { "X-Delete-Code": code },
    });
    if (response.status === 204) {
      $("delete-code").value = "";
      status.textContent = "Deleted. The link no longer works.";
      return;
    }
    status.textContent = response.status === 403 ? "That isn't this share's delete code." : (await detailOf(response)) || "That didn't work.";
  } catch {
    status.textContent = "Couldn't reach Unfret. Check your connection.";
  }
  $("delete-send").disabled = false;
});
