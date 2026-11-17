// Against a real server: UNFRET_LIVE_URL=https://dev.unfret.net npm run test:live. Makes a developer pass (one a day
// per network; set UNFRET_LIVE_PASS to reuse one) and checks the test shop: one deep check and one quick check.
import assert from "node:assert/strict";
import { test } from "node:test";

import { NoChecksLeftError, Unfret } from "../src/index.js";

const LIVE = process.env.UNFRET_LIVE_URL;

test("the test shop, deep and quick", { skip: !LIVE && "UNFRET_LIVE_URL not set" }, async (t) => {
  const unfret = new Unfret({ baseUrl: LIVE, pass: process.env.UNFRET_LIVE_PASS });
  if (!unfret.pass) {
    const issued = await unfret.newDeveloperPass();
    t.diagnostic(`developer pass ${issued.key_id}: ${issued.free_checks} free checks`);
  }
  try {
    const deep = await unfret.check(`${LIVE}/test-shop`);
    assert.ok(["allow", "warn", "block"].includes(deep.action));
    assert.equal(deep.verdict.tier, "deep");
    t.diagnostic(`deep: ${deep.action} (${deep.label}); ${deep.reasons.join(" | ")}`);
  } catch (error) {
    if (!(error instanceof NoChecksLeftError)) throw error;
    t.diagnostic("this network already had its developer pass today: set UNFRET_LIVE_PASS");
  }
  const before = (await unfret.checksLeft()).checks_remaining;
  const quick = await unfret.check(`${LIVE}/test-shop`, { tier: "quick" });
  assert.equal(quick.verdict.charged, false);
  assert.equal((await unfret.checksLeft()).checks_remaining, before);
});
