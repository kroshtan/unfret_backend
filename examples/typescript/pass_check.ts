// Check a page with an Unfret pass: POST /v1/check with `Authorization: Bearer ufk_…`.
//
// Without UNFRET_PASS it first makes a developer pass (20 free deep checks, one a day per network) by solving the
// server's proof of work, and prints it so you can reuse it. Run: `node pass_check.ts` (Node 22.18 or later).
import { createHash } from "node:crypto";

const API =
  process.env.UNFRET_API_URL ??
  (process.env.UNFRET_ENV === "production" ? "https://unfret.net" : "https://dev.unfret.net");
const PAGE = process.env.UNFRET_CHECK_URL ?? `${API}/test-shop`;
const TIER = process.env.UNFRET_TIER ?? "deep"; // deep uses one of the pass's checks; quick is free

// Find a solution: SHA-256(challenge + ":" + solution) starts with `bits` zero bits.
function solve(challenge: string, bits: number): string {
  for (let n = 0; ; n++) {
    const digest = createHash("sha256").update(`${challenge}:${n}`).digest();
    let zeros = 0;
    for (const byte of digest) {
      if (byte === 0) {
        zeros += 8;
        continue;
      }
      zeros += Math.clz32(byte) - 24;
      break;
    }
    if (zeros >= bits) return String(n);
  }
}

async function newDeveloperPass(): Promise<string> {
  const work = await (await fetch(`${API}/v1/keys/challenge?developer=true`)).json();
  const response = await fetch(`${API}/v1/keys`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ challenge: work.challenge, solution: solve(work.challenge, work.bits), developer: true }),
  });
  const issued = await response.json();
  if (!response.ok) throw new Error(`HTTP ${response.status} ${issued.error_code}: ${issued.detail}`);
  console.log(`New developer pass (${issued.free_checks} free checks). Keep it: export UNFRET_PASS=${issued.key}`);
  console.log(`Its recovery code, shown only once: ${issued.recovery_code}`);
  return issued.key;
}

const unfretPass = process.env.UNFRET_PASS || (await newDeveloperPass());
const response = await fetch(`${API}/v1/check`, {
  method: "POST",
  headers: { "Content-Type": "application/json", Authorization: `Bearer ${unfretPass}` },
  body: JSON.stringify({ url: PAGE, tier: TIER }),
  signal: AbortSignal.timeout(100_000), // a deep check answers within about a minute
});
const verdict = await response.json();
if (!response.ok) {
  console.error(`HTTP ${response.status} ${verdict.error_code}: ${verdict.detail}`);
  process.exit(1);
}
console.log(`${verdict.action.toUpperCase()} (${verdict.band}, risk ${verdict.risk_score.toFixed(2)}) for ${PAGE}`);
for (const flag of verdict.flags) console.log(`  - ${flag.type}: ${flag.evidence}`);
console.log({ charged: verdict.charged, free_check: verdict.free_check, free_checks_left: verdict.free_checks_left });
