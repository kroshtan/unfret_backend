// Making a new Unfret pass (an API key): a few seconds of proof of work in the browser, then POST /v1/keys. Shared by
// /checkout (a pass for a purchase or a code), /check (a free pass on a first visit) and /developers (a developer
// pass, whose challenge is harder). The browser extension makes its free pass on install the same way (its
// src/shared/new-pass.ts follows this file).

import { clearRef, storedRef } from "./ref.js";

// SHA-256 in plain JavaScript, for the proof of work only: Web Crypto's digest is asynchronous, and one await per hash
// made a developer pass's harder challenge take half a minute. The challenge's whole 64-byte blocks are hashed once;
// each counter then costs one or two blocks, with nothing allocated. The answer is the same first counter Web Crypto
// (and the server's check, unfret.credits.keys.pow_ok) gives.
const K = new Int32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01,
  0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
  0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116, 0x1e376c08,
  0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);
const IV = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
const W = new Int32Array(64);

// One 64-byte block of `bytes` at `off` into the state `h` (8 words), in place.
function block(h, bytes, off) {
  for (let i = 0; i < 16; i++) {
    const j = off + 4 * i;
    W[i] = (bytes[j] << 24) | (bytes[j + 1] << 16) | (bytes[j + 2] << 8) | bytes[j + 3];
  }
  for (let i = 16; i < 64; i++) {
    const x = W[i - 15];
    const y = W[i - 2];
    const s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
    const s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
    W[i] = (W[i - 16] + s0 + W[i - 7] + s1) | 0;
  }
  let [a, b, c, d, e, f, g, k] = h;
  for (let i = 0; i < 64; i++) {
    const s1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
    const t1 = (k + s1 + ((e & f) ^ (~e & g)) + K[i] + W[i]) | 0;
    const s0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
    const t2 = (s0 + ((a & b) ^ (a & c) ^ (b & c))) | 0;
    k = g;
    g = f;
    f = e;
    e = (d + t1) | 0;
    d = c;
    c = b;
    b = a;
    a = (t1 + t2) | 0;
  }
  h[0] = (h[0] + a) | 0;
  h[1] = (h[1] + b) | 0;
  h[2] = (h[2] + c) | 0;
  h[3] = (h[3] + d) | 0;
  h[4] = (h[4] + e) | 0;
  h[5] = (h[5] + f) | 0;
  h[6] = (h[6] + g) | 0;
  h[7] = (h[7] + k) | 0;
}

/**
 * The proof of work: the first counter `n` with SHA-256(challenge + ":" + n) starting with `bits` zero bits (at most
 * 32). Gives the page a turn every 50,000 tries, so it stays responsive.
 */
export async function solve(challenge, bits) {
  const prefix = new TextEncoder().encode(`${challenge}:`);
  const full = prefix.length - (prefix.length % 64);
  const mid = new Int32Array(IV);
  for (let off = 0; off < full; off += 64) block(mid, prefix, off);
  const rest = prefix.length - full;
  const tail = new Uint8Array(128);
  tail.set(prefix.subarray(full));
  const h = new Int32Array(8);
  for (let n = 0; ; n++) {
    if (n && n % 50000 === 0) await new Promise((resolve) => setTimeout(resolve, 0));
    const digits = String(n);
    let len = rest;
    for (let i = 0; i < digits.length; i++) tail[len++] = digits.charCodeAt(i);
    const end = len + 9 > 64 ? 128 : 64;
    tail.fill(0, len, end);
    tail[len] = 0x80;
    const bitLen = (prefix.length + digits.length) * 8;
    tail[end - 4] = bitLen >>> 24;
    tail[end - 3] = bitLen >>> 16;
    tail[end - 2] = bitLen >>> 8;
    tail[end - 1] = bitLen;
    h.set(mid);
    block(h, tail, 0);
    if (end === 128) block(h, tail, 64);
    const zeros = h[0] === 0 ? 32 + Math.clz32(h[1]) : Math.clz32(h[0]);
    if (zeros >= bits) return digits;
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
  // A developer pass needs a developer challenge: a harder proof of work (a few seconds more).
  const challenge = await api(developer ? "/v1/keys/challenge?developer=true" : "/v1/keys/challenge");
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
