"""A quick result's suggestion of a deep check, priced for whoever asks: the cached result keeps no caller's price."""

from typing import Any

import asyncpg
import httpx
import pytest

from tests.integration.conftest import FakeGraph
from tests.integration.test_keys import PAGE, PRICE, bearer, solve
from tests.unit.test_card import state, verdict
from unfret import card
from unfret.config import settings
from unfret.credits import ledger, packs
from unfret.facts import PageFacts
from unfret.graph import SKILLS
from unfret.main import app
from unfret.routes import analyze, check
from unfret.x402.verdict import skills_for

QUICK = {**PAGE, "tier": "quick"}
FACTS = PageFacts(title="Sweater", price=29.0, image_urls=["https://shop.test/s.jpg"])


@pytest.fixture(autouse=True)
def easy_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "key_pow_bits", 4)
    monkeypatch.setattr(analyze, "_quick_rate", analyze.RateLimit(1000))
    monkeypatch.setattr(check, "_rate", check.RateLimit(1000))


def quick_result(band: str) -> dict[str, Any]:
    reasons = () if band == "reliable" else ("big_discount",)
    out = card.complete(verdict(band, reasons), state({}, facts=FACTS), available=skills_for("quick", SKILLS),
                        applicable=SKILLS)  # fmt: skip
    assessment = {"risk_score": 0.6, "confidence": 0.7, "flags": [], "model_version": "v1-quick-test"}
    return {"score": 60, "explanation": "A quick look.", "page_type": "storefront", "verdict": out,
            "assessment": assessment}  # fmt: skip


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> FakeGraph:
    graph = FakeGraph()
    graph.result = quick_result("careful")
    monkeypatch.setattr(app, "quick_graph", graph)
    return graph


async def new_pass(client: httpx.AsyncClient, *, free_checks: bool = True) -> dict[str, Any]:
    challenge = (await client.get("/v1/keys/challenge")).json()
    body = {"challenge": challenge["challenge"], "solution": solve(challenge["challenge"], challenge["bits"])}
    response = await client.post("/v1/keys", json={**body, "free": free_checks})
    assert response.status_code == 201
    issued: dict[str, Any] = response.json()
    return issued


def offer(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()["verdict"]["deep_check"]
    return found


async def test_the_suggestion_says_what_a_deep_check_costs_each_caller(
    client: httpx.AsyncClient, pool: asyncpg.Pool, quick: FakeGraph
) -> None:
    smallest = packs.packs(settings)[0]
    price = card._dollars(smallest.card_usd_micros)
    anonymous = offer(await client.post("/v1/analyze", json=QUICK))
    assert anonymous["suggested"] and anonymous["reason"] == "on_the_fence"
    assert anonymous["text"].startswith("A deep check also looks at how old this shop is")
    assert (anonymous["cost"], anonymous["action"], anonymous["action_label"]) == (
        f"Deep checks come in packs, from {price} for {smallest.checks}.", "get_checks", "Get checks"
    )  # fmt: skip

    with_free = await new_pass(client)
    free_offer = offer(await client.post("/v1/analyze", json=QUICK, headers=bearer(with_free["key"])))
    assert (free_offer["cost"], free_offer["action"]) == ("Uses 1 of your 5 free deep checks.", "run_deep")
    assert free_offer["action_label"] == "Run a deep check"

    bought = await new_pass(client, free_checks=False)
    await ledger.credit(pool, bought["account"], 12 * PRICE)
    bought_offer = offer(await client.post("/v1/analyze", json=QUICK, headers=bearer(bought["key"])))
    assert bought_offer["cost"] == "Uses 1 check (you have 12)."

    empty = await new_pass(client, free_checks=False)
    assert offer(await client.post("/v1/analyze", json=QUICK, headers=bearer(empty["key"])))["action"] == "get_checks"
    assert len(quick.calls) == 1  # one analysis, from the cache after that, each priced for its own caller


async def test_no_price_and_no_button_without_a_suggestion(client: httpx.AsyncClient, quick: FakeGraph) -> None:
    quick.result = quick_result("junk_or_scam")
    serious = offer(await client.post("/v1/analyze", json=QUICK))
    assert serious == card.DeepCheck(suggested=False, reason="serious_concerns").model_dump(exclude_none=True)


async def test_a_quick_check_on_v1_check_with_a_pass_is_priced_for_the_pass(
    client: httpx.AsyncClient, quick: FakeGraph
) -> None:
    issued = await new_pass(client)
    response = await client.post("/v1/check", json=QUICK, headers=bearer(issued["key"]))
    assert response.status_code == 200, response.text
    found = response.json()["deep_check"]
    assert found["suggested"] and found["cost"] == "Uses 1 of your 5 free deep checks."
    assert found["adds"][0] == {"key": "shop_age", "area": "who", "label": "how old this shop is"}
