import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { test } from "node:test";

import {
  InvalidPassError,
  NoChecksLeftError,
  PageUnreadableError,
  RateLimitedError,
  Unfret,
  type Verdict,
  decide,
  solve,
} from "../src/index.js";

const VERDICT: Verdict = {
  action: "warn",
  assessed: true,
  risk_score: 0.5,
  confidence: 0.8,
  flags: [{ type: "timer_resets", evidence: "The countdown restarts.", weight: 1, label: "Fake timer" }],
  policy_applied: "balanced",
  tier: "deep",
  band: "careful",
  charged: true,
  free_checks_left: 19,
};

function fake(status = 200, body: unknown = VERDICT, seen: Request[] = []): typeof fetch {
  return async (input, init) => {
    seen.push(new Request(input, init));
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  };
}

test("the four verdicts map to actions", () => {
  const expected = { reliable: "allow", concerns: "warn", careful: "block", junk_or_scam: "block" } as const;
  for (const [band, action] of Object.entries(expected)) {
    assert.equal(decide({ ...VERDICT, band: band as Verdict["band"] }), action);
  }
});

test("the policy is configurable, and the rest stays", () => {
  assert.equal(decide({ ...VERDICT, band: "careful" }, { careful: "warn" }), "warn");
  assert.equal(decide({ ...VERDICT, band: "reliable" }, { careful: "warn" }), "allow");
});

test("an unassessed page is a warning by default", () => {
  const page = { ...VERDICT, assessed: false, band: null, risk_score: null };
  assert.equal(decide(page), "warn");
  assert.equal(decide(page, {}, "block"), "block");
});

test("check sends the pass and returns a decision", async () => {
  const seen: Request[] = [];
  const unfret = new Unfret({ pass: "ufk_x", baseUrl: "https://dev.unfret.net/", fetch: fake(200, VERDICT, seen) });
  const decision = await unfret.check("https://s.test/p");
  assert.equal(decision.action, "block");
  assert.equal(decision.label, "Think twice");
  assert.deepEqual(decision.reasons, ["Fake timer: The countdown restarts."]);
  assert.equal(decision.verdict.free_checks_left, 19);
  const request = seen[0]!;
  assert.equal(request.url, "https://dev.unfret.net/v1/check");
  assert.equal(request.headers.get("authorization"), "Bearer ufk_x");
  assert.deepEqual(await request.json(), { url: "https://s.test/p", tier: "deep" });
});

test("page content and the quick tier are sent", async () => {
  const seen: Request[] = [];
  await new Unfret({ pass: "ufk_x", fetch: fake(200, VERDICT, seen) }).check("https://s.test/p", {
    tier: "quick",
    pageContent: "<h1>x</h1>",
  });
  assert.deepEqual(await seen[0]!.json(), { url: "https://s.test/p", tier: "quick", page_content: "<h1>x</h1>" });
});

test("without a pass nothing is sent", async () => {
  const seen: Request[] = [];
  const saved = process.env.UNFRET_PASS;
  delete process.env.UNFRET_PASS;
  try {
    await assert.rejects(new Unfret({ fetch: fake(200, VERDICT, seen) }).check("https://s.test/p"), InvalidPassError);
  } finally {
    if (saved !== undefined) process.env.UNFRET_PASS = saved;
  }
  assert.equal(seen.length, 0);
});

test("refusals throw typed errors", async () => {
  const cases: [number, Record<string, unknown>, Function][] = [
    [401, { detail: "Unknown pass.", error_code: "INVALID_API_KEY" }, InvalidPassError],
    [402, { detail: "Not enough credits.", error_code: "INSUFFICIENT_CREDITS" }, NoChecksLeftError],
    [429, { detail: "Later.", error_code: "FREE_CHECKS_PAUSED", metadata: { retry_after_s: 60 } }, RateLimitedError],
    [422, { detail: "Could not fetch.", error_code: "PAGE_UNAVAILABLE" }, PageUnreadableError],
  ];
  for (const [status, body, kind] of cases) {
    await assert.rejects(new Unfret({ pass: "ufk_x", fetch: fake(status, body) }).check("https://s.test/p"), (e) => {
      assert.ok(e instanceof kind);
      assert.equal((e as { code: string }).code, body.error_code);
      if (status === 429) assert.equal((e as { retryAfter: number }).retryAfter, 60);
      return true;
    });
  }
});

test("solve finds the proof of work", async () => {
  const solution = await solve("abc", 8);
  assert.equal(createHash("sha256").update(`abc:${solution}`).digest()[0], 0);
});

test("a new developer pass is used from then on", async () => {
  const handler: typeof fetch = async (input, init) => {
    const request = new Request(input, init);
    const url = new URL(request.url);
    const path = url.pathname;
    if (path === "/v1/keys/challenge") {
      assert.equal(url.searchParams.get("developer"), "true"); // a developer pass needs the harder challenge
      return Response.json({ challenge: "c", bits: 12, expires_at: 0, developer: true });
    }
    if (path === "/v1/keys") {
      const body = (await request.json()) as { developer: boolean; solution: string };
      assert.equal(body.developer, true);
      const digest = createHash("sha256").update(`c:${body.solution}`).digest();
      assert.ok(digest[0] === 0 && (digest[1] ?? 255) < 16); // solved at the challenge's 12 bits
      return Response.json({ key: "ufk_new", key_id: "x", recovery_code: "ufr", free_checks: 20 }, { status: 201 });
    }
    assert.equal(request.headers.get("authorization"), "Bearer ufk_new");
    return Response.json(VERDICT);
  };
  const unfret = new Unfret({ fetch: handler });
  assert.equal((await unfret.newDeveloperPass()).free_checks, 20);
  assert.equal((await unfret.check("https://s.test/p")).action, "block");
});
