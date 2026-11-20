/**
 * A minimal client for Unfret: check a store or product page before an agent buys, and get allow, warn or block.
 *
 *     import { Unfret } from "./unfret"; // this file, copied into your project (it isn't on npm)
 *     const decision = await new Unfret({ pass: "ufk_…" }).check("https://shop.example/p/42");
 *     if (decision.action === "block") { ... }
 *
 * The action comes from the verdict a shopper would see (DEFAULT_POLICY): "Looks fine" → allow, "Worth a look" →
 * warn (ask your human), "Think twice" and "Serious concerns" → block; pass `policy` to change that.
 */

export type Action = "allow" | "warn" | "block";
export type Band = "reliable" | "concerns" | "careful" | "junk_or_scam";
export type Tier = "quick" | "deep";
export type Policy = Partial<Record<Band, Action>>;

/** One piece of evidence. */
export interface Flag {
  type: string;
  evidence: string;
  weight: number;
  label?: string | null;
  severity?: string | null;
}

/** Unfret's answer to `POST /v1/check` with a pass. */
export interface Verdict {
  action: Action;
  assessed?: boolean;
  risk_score: number | null;
  confidence: number;
  flags: Flag[];
  policy_applied: string;
  tier: Tier;
  cached?: boolean;
  band?: Band | null;
  charged?: boolean;
  free_check?: boolean;
  free_checks_left?: number;
  balance_usd_micros?: number;
  [field: string]: unknown;
}

/** What to do about a page, and why. */
export interface Decision {
  /** allow: go ahead. warn: ask your human first. block: don't buy. */
  action: Action;
  /** The verdict a shopper would see (null when the page couldn't be assessed). */
  band: Band | null;
  /** The verdict in words: "Looks fine", "Worth a look", "Think twice" or "Serious concerns". */
  label: string | null;
  /** The evidence, one line per flag. */
  reasons: string[];
  /** Unfret's whole answer. */
  verdict: Verdict;
}

export const DEFAULT_POLICY: Readonly<Record<Band, Action>> = {
  reliable: "allow", // Looks fine
  concerns: "warn", // Worth a look
  careful: "block", // Think twice
  junk_or_scam: "block", // Serious concerns
};

export const BAND_NAMES: Readonly<Record<Band, string>> = {
  reliable: "Looks fine",
  concerns: "Worth a look",
  careful: "Think twice",
  junk_or_scam: "Serious concerns",
};

const DEFAULT_URL = "https://unfret.net";
const TIMEOUT_MS = 120_000; // a deep check takes up to about a minute

/** Unfret refused or couldn't answer a call. Nothing was charged for any of these. */
export class UnfretError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly detail: string,
    readonly retryAfter: number | null = null,
  ) {
    super(detail ? `${code}: ${detail}` : code);
    this.name = new.target.name;
  }
}
/** The pass isn't valid (mistyped, or replaced with its backup code). */
export class InvalidPassError extends UnfretError {}
/** The pass has no deep checks left: buy more at unfret.net/checkout, or use the free quick tier. */
export class NoChecksLeftError extends UnfretError {}
/** Too many calls (retryAfter says how long to wait), or free deep checks are paused until 00:00 UTC. */
export class RateLimitedError extends UnfretError {}
/** Unfret couldn't fetch the page (or got a bot check): send its HTML as pageContent. */
export class PageUnreadableError extends UnfretError {}

/** Map Unfret's verdict to an action by its band (the four verdicts), with a policy. */
export function decide(verdict: Verdict, policy: Policy = {}, unassessed: Action = "warn"): Action {
  if (verdict.assessed === false) return unassessed;
  const rules: Policy = { ...DEFAULT_POLICY, ...policy };
  const band = verdict.band;
  if (band && rules[band]) return rules[band];
  // An answer without a band: Unfret's own action for the request's policy.
  return ["allow", "warn", "block"].includes(verdict.action) ? verdict.action : unassessed;
}

function toDecision(verdict: Verdict, policy: Policy, unassessed: Action): Decision {
  const band = verdict.band ?? null;
  return {
    action: decide(verdict, policy, unassessed),
    band,
    label: band ? BAND_NAMES[band] : null,
    reasons: (verdict.flags ?? []).map((f) => `${f.label || f.type}: ${f.evidence}`),
    verdict,
  };
}

async function raiseFor(response: Response): Promise<void> {
  if (response.ok) return;
  let body: { error_code?: string; detail?: string; metadata?: { retry_after_s?: number } } = {};
  try {
    body = (await response.json()) as typeof body;
  } catch {
    // not JSON: the status says enough
  }
  const code = String(body.error_code ?? response.status);
  const wait = body.metadata?.retry_after_s ?? Number(response.headers.get("retry-after") ?? NaN);
  const retryAfter = Number.isFinite(wait) ? Number(wait) : null;
  let Kind = UnfretError;
  if (response.status === 401) Kind = InvalidPassError;
  else if (code === "INSUFFICIENT_CREDITS" || response.status === 402) Kind = NoChecksLeftError;
  else if (response.status === 429) Kind = RateLimitedError;
  else if (code.startsWith("PAGE_")) Kind = PageUnreadableError;
  throw new Kind(response.status, code, String(body.detail ?? ""), retryAfter);
}

function leadingZeroBits(digest: Uint8Array): number {
  let bits = 0;
  for (const byte of digest) {
    if (byte === 0) {
      bits += 8;
      continue;
    }
    return bits + Math.clz32(byte) - 24;
  }
  return bits;
}

/** The proof of work `POST /v1/keys` asks for: SHA-256(challenge + ":" + solution) with `bits` zero bits. */
export async function solve(challenge: string, bits: number): Promise<string> {
  const encoder = new TextEncoder();
  const batch = 512;
  for (let start = 0; ; start += batch) {
    const digests = await Promise.all(
      Array.from({ length: batch }, (_, i) =>
        globalThis.crypto.subtle.digest("SHA-256", encoder.encode(`${challenge}:${start + i}`)),
      ),
    );
    const found = digests.findIndex((d) => leadingZeroBits(new Uint8Array(d)) >= bits);
    if (found >= 0) return String(start + found);
  }
}

export interface UnfretOptions {
  /** The Unfret pass (ufk_…); default: the UNFRET_PASS environment variable (Node). */
  pass?: string;
  /** The server; default UNFRET_API_URL or https://unfret.net (https://dev.unfret.net is the test server). */
  baseUrl?: string;
  /** Band → action, over DEFAULT_POLICY (e.g. { concerns: "allow" }). */
  policy?: Policy;
  /** The action for a page Unfret couldn't assess (default "warn"). */
  unassessed?: Action;
  /** A fetch to use (tests, proxies); default the global fetch. */
  fetch?: typeof fetch;
}

export interface CheckOptions {
  /** "deep" (everything; one check from the pass, the default) or "quick" (rules on the page; free). */
  tier?: Tier;
  /** The page's HTML, if you have it (needed for marketplaces that block server fetches). */
  pageContent?: string;
}

function env(name: string): string | undefined {
  const process = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process;
  return process?.env?.[name] || undefined;
}

/** Unfret's check, with an Unfret pass. */
export class Unfret {
  pass: string | undefined;
  readonly baseUrl: string;
  private readonly policy: Policy;
  private readonly unassessed: Action;
  private readonly fetch: typeof fetch;

  constructor(options: UnfretOptions = {}) {
    this.pass = options.pass ?? env("UNFRET_PASS");
    this.baseUrl = (options.baseUrl ?? env("UNFRET_API_URL") ?? DEFAULT_URL).replace(/\/+$/, "");
    this.policy = options.policy ?? {};
    this.unassessed = options.unassessed ?? "warn";
    this.fetch = options.fetch ?? globalThis.fetch.bind(globalThis);
  }

  private auth(): Record<string, string> {
    if (!this.pass) throw new InvalidPassError(401, "NO_PASS", "Pass an Unfret pass, or set UNFRET_PASS.");
    return { Authorization: `Bearer ${this.pass}` };
  }

  private request(path: string, init: RequestInit = {}): Promise<Response> {
    return this.fetch(`${this.baseUrl}${path}`, { signal: AbortSignal.timeout(TIMEOUT_MS), ...init });
  }

  /** Check a store or product page. Throws an UnfretError (never charged) when the call is refused. */
  async check(url: string, options: CheckOptions = {}): Promise<Decision> {
    const body: Record<string, string> = { url, tier: options.tier ?? "deep" };
    if (options.pageContent) body.page_content = options.pageContent;
    const response = await this.request("/v1/check", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...this.auth() },
      body: JSON.stringify(body),
    });
    await raiseFor(response);
    return toDecision((await response.json()) as Verdict, this.policy, this.unassessed);
  }

  /** What the pass has left (GET /v1/credits): checks_remaining, free_checks_remaining, balance_usd_micros, … */
  async checksLeft(): Promise<Record<string, unknown>> {
    const response = await this.request("/v1/credits", { headers: this.auth() });
    await raiseFor(response);
    return (await response.json()) as Record<string, unknown>;
  }

  /**
   * Make a developer pass and use it from now on: 20 free deep checks, for trying Unfret out; one per network a day.
   * Its free checks work on the API and the MCP server, not in the extension or on the website. Asks for a developer
   * challenge (GET /v1/keys/challenge?developer=true, a harder proof of work) and solves it: a few seconds. Keep `key`
   * and `recovery_code`: they are shown only this once.
   */
  async newDeveloperPass(): Promise<{ key: string; key_id: string; recovery_code: string; free_checks: number }> {
    const challenge = await this.request("/v1/keys/challenge?developer=true");
    await raiseFor(challenge);
    const work = (await challenge.json()) as { challenge: string; bits: number };
    const response = await this.request("/v1/keys", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        challenge: work.challenge,
        solution: await solve(work.challenge, work.bits),
        developer: true,
      }),
    });
    await raiseFor(response);
    const issued = (await response.json()) as {
      key: string;
      key_id: string;
      recovery_code: string;
      free_checks: number;
    };
    this.pass = issued.key;
    return issued;
  }
}
