"""The quick result's suggestion of a deep check: when, what it would add for this page, and what it costs."""

import json
import re
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_card import COPY, state, verdict
from unfret import card, public_result, scoring
from unfret.config import settings
from unfret.facts import PageFacts
from unfret.facts.schema import Review
from unfret.graph import SKILLS, build_graph
from unfret.prices import format_usd
from unfret.routes.pages import STATIC_DIR
from unfret.routes.test_shop import render
from unfret.x402.verdict import Verdict, skills_for, to_verdict

QUICK = skills_for("quick", SKILLS)
SHOP = PageFacts(title="Projector", price=49.0, image_urls=["https://shop.example/p.jpg"], brand="Lumo")
# A marketplace listing sold by the marketplace itself, and one sold by a reseller.
OWN = PageFacts(title="Kettle", price=20.0, platform="amazon", brand="Philips", seller="Amazon.com",
                image_urls=["https://m.example/k.jpg"])  # fmt: skip
RESOLD = OWN.model_copy(update={"seller": "BestDeals4U"})
# Captured at import, before the suite's autouse fixture hides the shipped models.
_LOAD_MODEL = scoring.load_model


def quick(band: str, facts: PageFacts = SHOP, page_type: str = "storefront", reasons: tuple[str, ...] = (),
          **extra: Any) -> dict[str, Any]:  # fmt: skip
    return card.complete(
        verdict(band, reasons), state({}, page_type, facts=facts, **extra), available=QUICK, applicable=SKILLS
    )


def without(out: dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in out.items() if k != "deep_check"}, sort_keys=True)


PAGES = [(SHOP, "storefront"), (OWN, "aggregate_listing"), (RESOLD, "aggregate_listing"), (PageFacts(), "storefront")]


@pytest.mark.parametrize("band", ["reliable", "concerns", "careful", "junk_or_scam"])
@pytest.mark.parametrize(("facts", "page_type"), PAGES)
def test_the_quick_verdict_is_byte_identical_with_or_without_the_hint(
    band: str, facts: PageFacts, page_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    reasons = () if band == "reliable" else ("big_discount",)
    with_hint = quick(band, facts, page_type, reasons)
    monkeypatch.setattr(card, "deep_check", lambda *_, **__: None)
    plain = quick(band, facts, page_type, reasons)
    assert plain["deep_check"] is None and with_hint["deep_check"] is not None
    assert without(with_hint) == without(plain)


async def test_a_real_quick_check_answers_the_same_with_or_without_the_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _LOAD_MODEL.__wrapped__(scoring.MODEL_QUICK_PATH)
    monkeypatch.setattr(scoring, "load_model", lambda path=scoring.MODEL_PATH: model)
    monkeypatch.setattr(settings, "fingerprints", False)
    monkeypatch.setattr(settings, "price_observations", False)
    pages = [
        ((Path(__file__).parent.parent / "fixtures/pages/shopify-productgroup-us.html").read_text(),
         "https://www.allbirds.com/products/mens-tree-runners"),
        (render("product.html"), "https://dev.unfret.net/test-shop"),
        (render("sister.html"), "https://dev.unfret.net/test-shop/sister"),
    ]  # fmt: skip

    async def run() -> list[dict[str, Any]]:
        graph = build_graph(QUICK, model_path=scoring.MODEL_QUICK_PATH, llm=False)
        return [await graph.ainvoke({"page_content": h, "url": u, "flags": [], "explain": True}) for h, u in pages]

    hinted = await run()
    monkeypatch.setattr(card, "deep_check", lambda *_, **__: None)
    plain = await run()
    assert [r["verdict"]["band"] for r in hinted] == ["concerns", "junk_or_scam", "careful"]
    assert [r["verdict"]["deep_check"]["suggested"] for r in hinted] == [True, False, True]
    for a, b in zip(hinted, plain, strict=True):
        assert (a["score"], without(a["verdict"])) == (b["score"], without(b["verdict"]))
        x402 = to_verdict(a, policy="balanced", tier="quick").model_dump(mode="json")
        x402_plain = to_verdict(b, policy="balanced", tier="quick").model_dump(mode="json")
        assert {**x402, "checked_at": None, "deep_check": None} == {**x402_plain, "checked_at": None}


def test_on_the_fence_a_deep_check_is_suggested_with_what_it_adds_here() -> None:
    for band in ("concerns", "careful"):
        offer = quick(band, reasons=("big_discount",))["deep_check"]
        assert (offer["suggested"], offer["reason"], offer["quiet"]) == (True, "on_the_fence", False)
        assert [a["key"] for a in offer["adds"]] == ["shop_age", "photo_elsewhere"]
        assert offer["text"] == (
            "A deep check also looks at how old this shop is and whether its product photo appears on other shops."
        )
        assert offer["cost"] is None and offer["action"] is None  # priced per caller, not kept with the result


def test_what_it_adds_follows_the_concern_and_what_the_page_shows() -> None:
    reviews = SHOP.model_copy(update={"reviews": [Review(body="Great", rating=5)]})
    offer = quick("concerns", reviews, reasons=("review_import_app",))["deep_check"]
    assert [a["key"] for a in offer["adds"]] == ["reviews", "shop_age"]  # the reviews area had the concern
    bare = quick("concerns", PageFacts(title="Lamp", price=9.0), reasons=("big_discount",))["deep_check"]
    assert [a["key"] for a in bare["adds"]] == ["shop_age"]  # no photo to search, no reviews, no brand
    assert bare["text"] == "A deep check also looks at how old this shop is."


def test_looks_fine_on_an_unknown_shop_or_a_resellers_listing_suggests_it_quietly() -> None:
    for facts, page_type in ((SHOP, "storefront"), (RESOLD, "aggregate_listing")):
        offer = quick("reliable", facts, page_type)["deep_check"]
        assert (offer["suggested"], offer["reason"], offer["quiet"]) == (True, "unknown_seller", True)
    listing = quick("reliable", RESOLD, "aggregate_listing")["deep_check"]
    assert [a["key"] for a in listing["adds"]] == ["photo_elsewhere", "brand"]  # no shop age on a marketplace


def test_no_suggestion_at_serious_concerns_or_on_a_marketplaces_own_listing_that_looks_fine() -> None:
    assert (
        quick("junk_or_scam", reasons=("big_discount",))["deep_check"]
        == card.DeepCheck(suggested=False, reason="serious_concerns").model_dump()
    )
    own = quick("reliable", OWN, "aggregate_listing")["deep_check"]
    assert (own["suggested"], own["reason"], own["adds"], own["text"]) == (False, "known_seller", [], "")
    brand_sells = OWN.model_copy(update={"seller": "Philips"})
    assert quick("reliable", brand_sells, "aggregate_listing")["deep_check"]["reason"] == "known_seller"
    # On the fence, even the marketplace's own listing gets it.
    assert quick("careful", OWN, "aggregate_listing", ("big_discount",))["deep_check"]["suggested"] is True
    nothing = PageFacts(title="Kettle", price=20.0, platform="amazon", seller="BestDeals4U")
    assert quick("concerns", nothing, "aggregate_listing", ("big_discount",))["deep_check"]["reason"] == (
        "nothing_to_add"
    )


def test_a_deep_result_has_no_suggestion() -> None:
    out = card.complete(verdict("careful", ("big_discount",)), state({}, facts=SHOP), available=SKILLS,
                        applicable=SKILLS)  # fmt: skip
    assert out["deep_check"] is None
    assert to_verdict({"score": 30, "verdict": out, "assessment": {"risk_score": 0.7, "confidence": 0.8}},
                      policy="balanced", tier="deep").deep_check is None  # fmt: skip


def test_an_ad_on_a_known_platform_gets_what_a_deep_check_of_an_ad_adds(monkeypatch: pytest.MonkeyPatch) -> None:
    ad = PageFacts(title="Bike", price=120.0, platform="marktplaats", image_urls=["https://m.example/b.jpg"])
    url = "https://www.marktplaats.nl/v/fietsen/m2112345678-bike"
    monkeypatch.setattr(settings, "expansion_classes", True)
    offer = quick("reliable", ad, url=url)["deep_check"]
    assert [a["key"] for a in offer["adds"]] == ["seller_profile", "ad_photos"]
    assert offer["text"] == (
        "A deep check also looks at the seller's profile and ratings and whether its photos appear in other ads."
    )
    monkeypatch.setattr(settings, "expansion_classes", False)  # then a deep check reads it as a shop's page too
    assert quick("reliable", ad, url=url)["deep_check"]["adds"][0]["key"] == "shop_age"


def test_the_cost_is_the_callers_and_the_price_comes_from_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert card.deep_check_cost(free_left=3) == ("Uses 1 of your 3 free deep checks.", "run_deep")
    assert card.deep_check_cost(free_left=1) == ("Uses your last free deep check.", "run_deep")
    assert card.deep_check_cost(free_left=2, paused=True, bought=12) == ("Uses 1 check (you have 12).", "run_deep")
    assert card.deep_check_cost() == ("Checks cost $0.10 each, from a $10 top-up.", "get_checks")
    assert card.deep_check_cost(free_left=2, paused=True)[0].startswith("Free deep checks are back at 00:00 UTC.")
    assert card.deep_check_cost(x402_usd_micros=100_000) == ("A deep check of this page costs $0.10.", "run_deep")

    monkeypatch.setattr(settings, "price_per_check_usd", Decimal("0.25"))
    monkeypatch.setattr(settings, "topup_min_amount", Decimal("12.50"))
    assert card.deep_check_cost()[0] == "Checks cost $0.25 each, from a $12.50 top-up."

    offer = card.DeepCheck.model_validate(quick("careful", reasons=("big_discount",))["deep_check"])
    priced = card.with_cost(offer, card.deep_check_cost())
    assert (priced.action, priced.action_label) == ("get_checks", "Get checks")
    assert card.with_cost(offer, card.deep_check_cost(free_left=3)).action_label == "Run a deep check"
    unsuggested = card.DeepCheck(suggested=False, reason="serious_concerns")
    assert card.with_cost(unsuggested, card.deep_check_cost()) == unsuggested  # nothing to price, no button


def test_an_x402_quick_answer_carries_the_suggestion_at_the_deep_price() -> None:
    out = quick("careful", reasons=("big_discount",))
    answer = to_verdict({"score": 30, "verdict": out, "assessment": {"risk_score": 0.7, "confidence": 0.6}},
                        policy="balanced", tier="quick")  # fmt: skip
    assert answer.deep_check is not None and answer.deep_check.suggested
    price = format_usd(settings.x402_price_deep_usd_micros)
    assert answer.deep_check.cost == f"A deep check of this page costs {price}."
    assert answer.deep_check.action == "run_deep"
    Verdict.model_validate(answer.model_dump(mode="json"))
    # An answer from before the suggestion (the verdict store, the cache) still reads.
    older = answer.model_dump(mode="json")
    del older["deep_check"]
    assert Verdict.model_validate(older).deep_check is None


def test_the_copy_is_in_the_catalogue_and_never_calls_names() -> None:
    copy = COPY["deep_check"]
    keys = {k for extras in card.DEEP_EXTRAS.values() for k, _, _ in extras}
    assert set(copy["adds"]) == keys
    for page_type, extras in card.DEEP_EXTRAS.items():
        kind = card.kind_of(page_type)
        assert {a for _, areas, _ in extras for a in areas} <= set(COPY["checklist"]["areas"][kind]), page_type
    assert set(copy["actions"]) == {"run_deep", "get_checks", "donate"}
    assert not public_result.FORBIDDEN_RE.search(json.dumps(copy))
    # No urgency: no countdowns, deadlines or pressure words in the suggestion.
    assert not re.search(r"\b(now|hurry|today|last chance|limited|only)\b", json.dumps(copy), re.IGNORECASE)


@pytest.mark.usefixtures("node")
def test_the_site_shows_the_servers_suggestion_once_and_nothing_without_it(tmp_path: Path) -> None:
    for name in ("check-render.js", "check-core.js"):
        (tmp_path / name).write_text((STATIC_DIR / name).read_text())
    offer = card.with_cost(
        card.DeepCheck.model_validate(quick("careful", reasons=("big_discount",))["deep_check"]),
        card.deep_check_cost(),
    ).model_dump()
    cases = [
        {"tier": "quick", "verdict": {"deep_check": offer}},
        {"tier": "quick", "verdict": {"deep_check": {**offer, "action": "run_deep", "action_label": None}}},
        {"tier": "quick", "verdict": {"deep_check": {"suggested": False, "reason": "serious_concerns"}}},
        {"tier": "quick", "verdict": {}},  # an older server
        {"tier": "deep", "verdict": {"deep_check": offer}},
        {"tier": "quick", "score": -1},  # not assessed
    ]
    script = (
        f"import {{ deepHintOf }} from {json.dumps((tmp_path / 'check-render.js').as_uri())};"
        f"process.stdout.write(JSON.stringify({json.dumps(cases)}.map(deepHintOf)));"
    )
    run = subprocess.run(["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True)
    out = json.loads(run.stdout)
    assert out[0] == {"text": offer["text"], "cost": offer["cost"], "action": "get_checks", "label": "Get checks",
                      "quiet": False}  # fmt: skip
    assert out[1]["label"] == "Run a deep check"
    assert out[2:] == [None, None, None, None]
