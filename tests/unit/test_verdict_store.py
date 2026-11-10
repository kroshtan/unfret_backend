"""The known-listing fast path: what is kept, when it is served, and that callers can't plant an answer."""

from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from solders.keypair import Keypair

from tests.unit.x402_helpers import (
    FakeRpc,
    configured,
    expected,
    make_x402,
    payment_header,
    payment_instructions,
    sign_payment,
)
from unfret import shop_store, verdict_store
from unfret.facts import PageFacts
from unfret.main import app
from unfret.verdict_store import MemoryVerdictStore, Stored, listing_key
from unfret.x402.verdict import Verdict

HOT = Keypair()
COLD = Keypair().pubkey()
TODAY = date(2026, 11, 9)
URL = "https://glowlamp.shop/products/lamp"
SHOP_HTML = "<html><script>fbq('init', '1234509876543')</script><h1>Glow Lamp</h1></html>"


class AssessingGraph:
    """Returns an assessed result for the page it gets, and records the shop as the real judge does."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.risk = 0.1
        self.degraded = False

    async def ainvoke(self, state: dict[str, Any], config: Any = None) -> dict[str, Any]:
        self.calls.append(state)
        result = {
            **state,
            "score": 85,
            "explanation": "x",
            "skill_results": [],
            "page_type": "storefront",
            "facts": PageFacts(platform="shopify"),
            "assessment": {
                "risk_score": self.risk,
                "confidence": 0.8,
                "degraded": self.degraded,
                "model_version": "c",
            },
            "verdict": {"band": "reliable", "headline": "Looks fine.", "reasons": []},
        }
        await shop_store.observe(result)
        return result


@pytest.fixture
def graph() -> AssessingGraph:
    return AssessingGraph()


@pytest.fixture
def rpc() -> FakeRpc:
    return FakeRpc()


@pytest.fixture(autouse=True)
def stores(monkeypatch: pytest.MonkeyPatch) -> MemoryVerdictStore:
    store = MemoryVerdictStore()
    verdict_store.use_store(store)
    shop_store.use_store(shop_store.MemoryShopStore())
    monkeypatch.setattr(shop_store, "today", lambda: TODAY)
    return store


@pytest.fixture
async def client(
    rpc: FakeRpc, graph: AssessingGraph, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(app, "x402", make_x402(configured(HOT, COLD), rpc))
    monkeypatch.setattr(app, "quick_graph", graph)
    monkeypatch.setattr(app, "graph", graph)
    fetched: list[str] = []

    async def fetch(url: str, **_: object) -> bytes:
        fetched.append(url)
        return SHOP_HTML.encode()

    monkeypatch.setattr("unfret.routes.check.fetch_public", fetch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        c.fetched = fetched  # type: ignore[attr-defined]
        yield c


def paid() -> dict[str, str]:
    payer = Keypair()  # a new payer each time: the same payment twice would be a retry
    tx = sign_payment(payment_instructions(payer.pubkey(), HOT.pubkey(), 100_000), HOT.pubkey(), [payer])
    return {"PAYMENT-SIGNATURE": payment_header(tx, expected(HOT.pubkey()))}


def check(**extra: Any) -> dict[str, Any]:
    return {"url": URL, "tier": "quick", "policy": "balanced", **extra}


def test_listing_keys_ignore_tracking_but_keep_the_product() -> None:
    base = listing_key("https://www.glowlamp.shop/products/Lamp/?utm_source=x&fbclid=1")
    assert base == listing_key("https://glowlamp.shop/products/lamp")
    assert listing_key("https://shop.test/p.php?id=5&ref=a") == listing_key("https://shop.test/p.php?id=5")
    assert listing_key("https://shop.test/p.php?id=5") != listing_key("https://shop.test/p.php?id=6")
    assert listing_key("not a url") is None


async def test_a_fetched_listing_is_answered_from_the_fast_path(
    client: httpx.AsyncClient, graph: AssessingGraph, rpc: FakeRpc
) -> None:
    first = (await client.post("/v1/check", json=check(), headers=paid())).json()
    second = (await client.post("/v1/check", json=check(policy="strict"), headers=paid())).json()

    assert (first["source"], second["source"]) == ("analysis", "fingerprint")
    assert len(graph.calls) == 1 and client.fetched == [URL]  # type: ignore[attr-defined]
    assert len(rpc.sent) == 2  # the fast path is still paid for
    assert second["risk_score"] == first["risk_score"] and second["cached"] is True
    assert (second["policy_applied"], second["action"]) == ("strict", "allow")


async def test_caller_supplied_content_never_plants_an_answer(
    client: httpx.AsyncClient, graph: AssessingGraph, stores: MemoryVerdictStore
) -> None:
    # A crafted upload that scores as harmless must not become the answer later callers get for this listing.
    planted = await client.post("/v1/check", json=check(page_content=SHOP_HTML + "<p>trust me</p>"), headers=paid())
    assert planted.json()["source"] == "analysis"
    assert stores.rows == {}

    honest = (await client.post("/v1/check", json=check(), headers=paid())).json()
    assert honest["source"] == "analysis" and len(graph.calls) == 2


async def test_a_kept_answer_is_never_served_instead_of_caller_content(
    client: httpx.AsyncClient, graph: AssessingGraph
) -> None:
    await client.post("/v1/check", json=check(), headers=paid())
    again = (await client.post("/v1/check", json=check(page_content=SHOP_HTML), headers=paid())).json()
    assert again["source"] == "analysis" and len(graph.calls) == 2


async def test_degraded_analyses_are_not_kept(
    client: httpx.AsyncClient, graph: AssessingGraph, stores: MemoryVerdictStore
) -> None:
    graph.degraded = True
    await client.post("/v1/check", json=check(), headers=paid())
    assert stores.rows == {}


async def test_a_changed_shop_falls_through(client: httpx.AsyncClient, graph: AssessingGraph) -> None:
    await client.post("/v1/check", json=check(), headers=paid())
    # Another analysis sees the shop with a different pixel: the kept answer no longer describes it.
    await shop_store.observe(
        {
            "url": URL,
            "page_type": "storefront",
            "facts": PageFacts(platform="shopify"),
            "page_content": "<script>fbq('init', '9999999999999')</script>",
        }
    )
    again = (await client.post("/v1/check", json=check(), headers=paid())).json()
    assert again["source"] == "analysis" and len(graph.calls) == 2


def kept(day: date) -> Stored:
    verdict = Verdict(
        action="allow",
        risk_score=0.5,
        confidence=0.7,
        flags=[],
        policy_applied="lenient",
        tier="quick",
        checked_at="2026-11-08T10:00:00Z",  # type: ignore[arg-type]
    )
    return Stored(day, None, verdict.model_dump(mode="json"))


async def test_answers_are_served_for_a_day_with_the_callers_policy(stores: MemoryVerdictStore) -> None:
    key = listing_key("https://www.amazon.nl/dp/B06X9STHNG")
    assert key is not None
    await stores.put(key, "quick", kept(TODAY - timedelta(days=1)))
    served = await verdict_store.recall("https://www.amazon.nl/dp/B06X9STHNG", "quick", "strict")
    assert served is not None and (served.action, served.source) == ("block", "fingerprint")
    assert await verdict_store.recall("https://www.amazon.nl/dp/B06X9STHNG", "deep", "strict") is None

    await stores.put(key, "quick", kept(TODAY - timedelta(days=2)))
    assert await verdict_store.recall("https://www.amazon.nl/dp/B06X9STHNG", "quick", "strict") is None
    await stores.purge(TODAY)
    assert stores.rows == {}


def test_only_assessed_undegraded_fetched_analyses_are_keepable() -> None:
    verdict = Verdict.model_validate(kept(TODAY).verdict)
    ok = {"assessment": {"risk_score": 0.5, "degraded": False}}
    assert verdict_store.keepable(ok, verdict, fetched_by_us=True)
    assert not verdict_store.keepable(ok, verdict, fetched_by_us=False)
    assert not verdict_store.keepable({"assessment": {"degraded": True}}, verdict, fetched_by_us=True)
    assert not verdict_store.keepable({}, verdict, fetched_by_us=True)
    unassessed = verdict.model_copy(update={"assessed": False})
    assert not verdict_store.keepable(ok, unassessed, fetched_by_us=True)
