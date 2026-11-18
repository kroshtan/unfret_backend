"""
The shop fingerprint store is guarded like the photo, price and review stores (:mod:`unfret.observations`).

A forged page (client-supplied content claiming to be any shop, with any pixel, payment account or address) alone
must never make another shop look linked to young or bad shops, plant an identifier on another shop, make an address
look shared or an identifier look like an agency's. Identifiers seen on pages Unfret fetched, or kept from before,
count as they always did, and so quick and deep answers for pages Unfret fetched are unchanged. The shops here are
made up.
"""

import json
from datetime import date, timedelta
from typing import Any

import pytest
from pydantic import SecretStr

from unfret import fact_cache, observations, scoring, shop_store
from unfret.config import settings
from unfret.facts import PageFacts
from unfret.graph import SKILLS, build_graph
from unfret.graph.skills import Skill
from unfret.graph.skills.shop_links import ShopLinksSkill
from unfret.observations import CLIENT, DEEP, FETCHED, LEGACY, QUICK, Kept, Provenance
from unfret.shop_store import MemoryShopStore, ShopRecord
from unfret.x402.verdict import skills_for, to_verdict

_LOAD_MODEL = scoring.load_model  # the real one, captured before the suite's autouse fixture hides it
TODAY = date(2026, 11, 18)
PIXEL = "7712309876541"
OTHER_PIXEL = "5550001112223"


def page(pixel: str = PIXEL, address: str = "") -> str:
    """A shop's product page with a Meta pixel (and an address in its footer)."""
    ld = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Fern Lamp",
        "offers": {"@type": "Offer", "price": "49.00", "priceCurrency": "EUR"},
    }
    return (
        f"<html><head><title>Fern Lamp</title><script>fbq('init', '{pixel}');</script>"
        f'<script type="application/ld+json">{json.dumps(ld)}</script></head>'
        f"<body><h1>Fern Lamp</h1><p>€49.00</p><button>Add to cart</button>"
        f"<p>{'A warm lamp for a reading corner. ' * 30}</p><footer>{address}</footer></body></html>"
    )


def state(domain: str, html: str = "", addresses: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "page_content": html or page(),
        "url": f"https://www.{domain}/products/fern-lamp",
        "flags": [],
        "facts": PageFacts(platform="generic", addresses=list(addresses)),
        "cleaned_content": "",
        "page_type": "storefront",
    }


def client(network: str, account: str | None = None, tier: observations.Tier = "deep") -> Provenance:
    """A check of content the caller supplied."""
    return observations.for_request(network, account, fetched=False, tier=tier)


def fetched(network: str = "198.51.100.1", tier: observations.Tier = "deep") -> Provenance:
    return observations.for_request(network, None, fetched=True, tier=tier)


async def observe(prov: Provenance | None, st: dict[str, Any]) -> None:
    """Record a shop as a finished check with this provenance would (none: outside a check, as before)."""
    if prov is None:
        await shop_store.observe(st)
        return
    with observations.observed_as(prov):
        shop_store.observe_later(st)
    await shop_store.drain()


async def linked(domain: str, html: str = "") -> list[str]:
    page_links = await shop_store.page_links(state(domain, html))
    assert page_links is not None
    return [link.domain for link in await shop_store.store().linked(page_links.domain, page_links.links)]


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch) -> MemoryShopStore:
    store = MemoryShopStore()
    shop_store.use_store(store)
    fact_cache.use_store(fact_cache.MemoryStore())
    monkeypatch.setattr(settings, "contact_hash_key", SecretStr("test-contact-key"))
    monkeypatch.setattr(settings, "fingerprints", True)
    monkeypatch.setattr(shop_store, "today", lambda: TODAY)
    monkeypatch.setattr(observations, "witnesses", observations.Witnesses())
    monkeypatch.setattr(observations, "writes", observations.WriteLimit(observations.witnesses))
    monkeypatch.setattr(settings, "observation_min_sources", 3)
    monkeypatch.setattr(settings, "observation_pages_per_network_per_day", 300)
    return store


async def test_a_forged_page_alone_doesnt_link_a_shop_to_another(fresh: MemoryShopStore) -> None:
    # Anyone can send "young-scam.shop's page" with lantern-house.shop's pixel on it.
    await observe(client("203.0.113.7", "pass-a"), state("young-scam.shop"))
    assert await linked("lantern-house.shop") == []
    assert fresh.kept[("meta_pixel", PIXEL, "young-scam.shop")] == Kept(CLIENT | DEEP, 1)

    # More passes from the same network, or the same pass from other networks, add no source.
    await observe(client("203.0.113.7", "pass-b"), state("young-scam.shop"))  # the same network
    await observe(client("192.0.2.40", "pass-a"), state("young-scam.shop"))  # same pass
    assert fresh.kept[("meta_pixel", PIXEL, "young-scam.shop")].sources == 1
    assert await linked("lantern-house.shop") == []

    # Independent sources corroborate it.
    await observe(client("192.0.2.41", "pass-c", "quick"), state("young-scam.shop"))
    await observe(client("198.18.0.9", "pass-d"), state("young-scam.shop"))
    assert fresh.kept[("meta_pixel", PIXEL, "young-scam.shop")] == Kept(CLIENT | DEEP | QUICK, 3)
    assert await linked("lantern-house.shop") == ["young-scam.shop"]


async def test_a_page_unfret_fetched_counts_at_once_and_old_rows_count_as_before(fresh: MemoryShopStore) -> None:
    await observe(fetched(tier="quick"), state("sister.shop"))
    assert fresh.kept[("meta_pixel", PIXEL, "sister.shop")] == Kept(FETCHED | QUICK, 1)
    assert await linked("lantern-house.shop") == ["sister.shop"]

    # A row from before provenance was kept (no provenance: LEGACY) counts, and keeps counting when seen again.
    fresh.links[("meta_pixel", PIXEL, "old-sister.shop")] = (TODAY - timedelta(days=30), TODAY - timedelta(days=30))
    fresh.shops["old-sister.shop"] = shop_store._Shop(TODAY, TODAY, ShopRecord("old-sister.shop", TODAY))
    assert set(await linked("lantern-house.shop")) == {"sister.shop", "old-sister.shop"}
    await observe(client("203.0.113.7"), state("old-sister.shop"))
    assert fresh.kept[("meta_pixel", PIXEL, "old-sister.shop")] == Kept(LEGACY | CLIENT | DEEP, 1)
    assert set(await linked("lantern-house.shop")) == {"sister.shop", "old-sister.shop"}


async def test_a_forged_page_cant_plant_an_identifier_on_another_shop(fresh: MemoryShopStore) -> None:
    await observe(fetched(), state("young-scam.shop", page(OTHER_PIXEL)))
    # "lantern-house.shop's page", forged, with the scam's pixel: lantern-house's own lookup doesn't use it.
    await observe(client("203.0.113.7"), state("lantern-house.shop", page(OTHER_PIXEL)))
    links = await shop_store.store().linked("lantern-house.shop", {})
    assert links == []
    # The scam's own check doesn't see lantern-house either.
    assert await linked("young-scam.shop", page(OTHER_PIXEL)) == []


async def test_forged_pages_dont_make_an_address_look_shared_or_an_id_look_like_an_agencys(
    fresh: MemoryShopStore,
) -> None:
    address = "Keizersgracht 1, 1015 CJ Amsterdam"
    for i in range(4):  # one network, many made-up shops giving lantern-house's address
        await observe(client("203.0.113.7", f"pass-{i}"), state(f"fake-{i}.shop", page(OTHER_PIXEL), (address,)))
    page_links = await shop_store.page_links(state("lantern-house.shop", addresses=(address,)))
    assert page_links is not None and page_links.links["address"]
    assert await shop_store.store().address_shops("lantern-house.shop", page_links.links["address"]) == set()

    # An identifier forged onto more than MAX_SHARE shops doesn't stop a real link from counting.
    await observe(fetched(), state("sister.shop", page(OTHER_PIXEL)))
    for i in range(shop_store.MAX_SHARE + 1):
        await observe(client(f"10.{i}.0.1", f"p{i}"), state(f"agency-{i}.shop", page(OTHER_PIXEL)))
    assert "sister.shop" in await linked("lantern-house.shop", page(OTHER_PIXEL))


async def test_only_a_page_unfret_fetched_changes_a_shops_facts(fresh: MemoryShopStore) -> None:
    await observe(fetched(), state("lantern-house.shop"))
    digest = fresh.shops["lantern-house.shop"].rec.fingerprint
    assert digest is not None
    await observe(client("203.0.113.7"), state("lantern-house.shop", page(OTHER_PIXEL)))
    assert fresh.shops["lantern-house.shop"].rec.fingerprint == digest  # the known-listing fast path keeps its key
    await observe(client("203.0.113.7"), state("new.shop"))
    assert fresh.shops["new.shop"].rec.fingerprint is None and fresh.shops["new.shop"].rec.apps == ()
    await observe(fetched(), state("lantern-house.shop", page(OTHER_PIXEL)))
    assert fresh.shops["lantern-house.shop"].rec.fingerprint != digest


async def test_past_the_networks_daily_limit_nothing_is_recorded(
    fresh: MemoryShopStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "observation_pages_per_network_per_day", 2)
    for i in range(3):
        await observe(client("203.0.113.7", tier="quick"), state(f"s{i}.shop"))
    assert set(fresh.shops) == {"s0.shop", "s1.shop"}
    for i in range(3, 6):  # a deep check of a page Unfret fetched: no limit, as before
        await observe(fetched("203.0.113.7"), state(f"s{i}.shop"))
    assert {"s3.shop", "s4.shop", "s5.shop"} <= set(fresh.shops)


# ── Quick and deep answers for pages Unfret fetched are unchanged ────────────────────────────────────────────


def _answer(result: dict[str, Any], tier: observations.Tier) -> dict[str, Any]:
    verdict = to_verdict(result, policy="balanced", tier=tier).model_dump(mode="json")
    verdict.pop("checked_at")
    kept = {k: result.get(k) for k in ("score", "explanation", "page_type", "assessment", "features")}
    return {"verdict": verdict, **kept, "findings": [f.model_dump() for f in result["findings"]]}


@pytest.mark.parametrize("tier", ["quick", "deep"])
async def test_answers_for_pages_unfret_fetched_are_as_before(
    tier: observations.Tier, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_path = scoring.MODEL_QUICK_PATH if tier == "quick" else scoring.MODEL_PATH
    model = _LOAD_MODEL.__wrapped__(model_path)
    monkeypatch.setattr(scoring, "load_model", lambda path=scoring.MODEL_PATH: model)
    skills: list[Skill] = skills_for("quick", SKILLS) if tier == "quick" else [ShopLinksSkill()]
    graph = build_graph(skills, model_path=model_path, llm=False)
    await fact_cache._store.put("rdap", "young-sister.shop", {"registered": str(TODAY - timedelta(days=20))}, 60)
    await fact_cache._store.put("rdap", "old-sister.shop", {"registered": "2014-03-01"}, 60)

    answers = []
    for record_as in (None, fetched(tier="quick"), fetched(tier="deep")):  # None: before provenance was kept
        shop_store.use_store(MemoryShopStore())
        await observe(record_as, state("young-sister.shop"))
        await observe(record_as, state("old-sister.shop"))
        with observations.observed_as(fetched("192.0.2.1", tier)):
            st = {"page_content": page(), "url": "https://www.lantern-house.shop/products/fern-lamp", "flags": []}
            result = await graph.ainvoke({**st, "explain": False})
        await shop_store.drain()
        assert result["features"]["shop_links.linked_shops"] == 2
        assert result["features"]["shop_links.linked_young_shops"] == 1
        answers.append(_answer(result, tier))
    assert answers[0] == answers[1] == answers[2]
