// A showcase case's page (/showcase/{id}): the server puts the case's facts and verdict in the HTML and the full
// result in a data block; this renders the result exactly like the link checker does, with the "Why?" details.
import { resultNodes } from "./check-render.js";

const data = document.getElementById("case-data");
const body = document.getElementById("result-body");
try {
  const result = JSON.parse(data?.textContent ?? "null");
  if (result?.verdict) {
    const { nodes, details } = resultNodes(result, {
      band: result.verdict.band,
      label: result.verdict.label,
      symbol: result.verdict.symbol,
    });
    if (details) nodes.push(details);
    body.replaceChildren(...nodes);
  }
} catch {
  // the server-rendered verdict stays
}
