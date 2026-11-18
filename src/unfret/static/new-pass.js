// Making a new Unfret pass (an API key): a few seconds of proof of work in the browser, then POST /v1/keys. Shared by
// /checkout (a pass for a purchase or a code) and /check (a free pass on a first visit). The browser extension makes
// its free pass on install the same way (its src/shared/new-pass.ts follows this file).

import { clearRef, storedRef } from "./ref.js";

function zeroBits(digest) {
  const bytes = new Uint8Array(digest);
  let bits = 0;
  for (const byte of bytes) {
    if (byte) return bits + Math.clz32(byte) - 24;
    bits += 8;
  }
  return bits;
}

/** The proof of work: a counter `n` with SHA-256(challenge + ":" + n) starting with `bits` zero bits, in batches. */
export async function solve(challenge, bits) {
  const encoder = new TextEncoder();
  for (let start = 0; ; start += 256) {
    const batch = Array.from({ length: 256 }, (_, i) => String(start + i));
    const digests = await Promise.all(
      batch.map((n) => crypto.subtle.digest("SHA-256", encoder.encode(`${challenge}:${n}`))),
    );
    const found = digests.findIndex((digest) => zeroBits(digest) >= bits);
    if (found >= 0) return batch[found];
  }
}

/**
 * A new pass: { key, recovery_code, key_id, account, balance_usd_micros, free_checks }.
 *
 * @param {(path: string, init?: RequestInit) => Promise<any>} api - Calls the API, throwing on an error status.
 * @param {{free?: boolean, developer?: boolean}} [options] - free: ask for the free checks a new pass comes with (once
 *   per browser); developer: a developer pass's free checks instead (/developers).
 * @returns {Promise<object>} The pass and its backup code, shown this once.
 */
export async function makePass(api, { free = false, developer = false } = {}) {
  const challenge = await api("/v1/keys/challenge");
  const solution = await solve(challenge.challenge, challenge.bits);
  // An affiliate's link brought this visitor (ref.js): the new pass counts for them, then the code is forgotten.
  const ref = storedRef();
  const body = {
    challenge: challenge.challenge,
    solution,
    ...(developer ? { developer: true } : free ? { free: true } : {}),
    ...(ref ? { ref } : {}),
  };
  const made = await api("/v1/keys", { method: "POST", body: JSON.stringify(body) });
  if (ref) clearRef();
  return made;
}
