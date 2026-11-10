"""US market support: Amazon.com and Walmart facts, US-shop identity, shipping and return terms, no EU rules."""

import json
from datetime import date
from typing import Any

import pytest

from unfret import scoring, shopper
from unfret.facts import PageFacts, parse_page
from unfret.graph.nodes import classify_node
from unfret.graph.skills.dark_patterns import DarkPatternsSkill, checkout_tax
from unfret.graph.skills.eu_registry import EuRegistrySkill
from unfret.graph.skills.shop_policies import ShopPoliciesSkill, longest_delivery_days
from unfret.graph.skills.us_identity import UsIdentitySkill, find_identity
from unfret.graph.state import Finding
from unfret.market import shop_market
from unfret.price_store import product_id


def state(facts: PageFacts, text: str = "", **extra: Any) -> Any:
    return {
        "page_content": "",
        "url": extra.pop("url", "https://shop.example.com/products/thing"),
        "flags": [],
        "facts": facts,
        "cleaned_content": text,
        "page_type": "storefront",
        **extra,
    }


US = PageFacts(currency="USD", price=20.0)
EU = PageFacts(currency="EUR", price=20.0)


# ── Facts ─────────────────────────────────────────────────────────────────────

# The current amazon.com buy box: label and text are sibling elements that share the feature name.
AMAZON_COM_PAGE = """
<html><head><link rel="canonical" href="https://www.amazon.com/dp/B0TESTUS01"></head><body><div id="dp">
<span id="productTitle">Etta James Love's Been Rough On Me</span>
<div id="bylineInfo">Etta James (Artist) Format: Audio CD</div>
<div id="corePrice_feature_div"><span class="a-offscreen">$10.90</span></div>
<span class="basisPrice">List Price:
<span class="a-price a-text-price"><span class="a-offscreen">$14.39</span></span></span>
<div class="offer-display-feature-label" offer-display-feature-name="desktop-fulfiller-info">
  <span>Ships from</span></div>
<div class="offer-display-feature-text" offer-display-feature-name="desktop-fulfiller-info">
  <span class="offer-display-feature-text-message">Amazon</span></div>
<div class="offer-display-feature-label" offer-display-feature-name="desktop-merchant-info"><span>Sold by</span></div>
<div class="offer-display-feature-text" offer-display-feature-name="desktop-merchant-info">
  <span class="offer-display-feature-text-message">RapidPrimePros</span></div>
<div data-hook="review"><i data-hook="review-star-rating"><span class="a-icon-alt">5.0 out of 5 stars</span></i>
<span data-hook="review-date">Reviewed in the United States on March 3, 2025</span>
<span data-hook="avp-badge-linkless">Verified Purchase</span><span data-hook="review-body">Great.</span></div>
</div></body></html>
"""


def test_amazon_com_buybox_byline_and_verified_badge() -> None:
    facts, _ = parse_page(AMAZON_COM_PAGE, "https://www.amazon.com/dp/B0TESTUS01")
    assert facts.platform == "amazon"
    assert (facts.price, facts.list_price, facts.currency) == (10.9, 14.39, "USD")
    assert (facts.ships_from, facts.seller) == ("Amazon", "RapidPrimePros")
    assert facts.brand == "Etta James (Artist)"
    assert facts.reviews[0].verified is True
    assert facts.reviews[0].posted == date(2025, 3, 3)


def _walmart_page(product: dict[str, Any], reviews: dict[str, Any] | None = None) -> str:
    data = {"props": {"pageProps": {"initialData": {"data": {"product": product, "reviews": reviews or {}}}}}}
    # Walmart's JSON-LD can lead with a protection plan's price: the page data must win.
    ld = {"@type": "Product", "name": product.get("name"), "offers": [{"price": 16, "priceCurrency": "USD"}]}
    return (
        f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head><body>'
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
        '<script>var p = {"pageId":"global_evergreen"};</script></body></html>'
    )


WALMART_PRODUCT = {
    "name": "Apple AirPods 4",
    "brand": "Apple",
    "priceInfo": {"currentPrice": {"price": 89.0, "currencyUnit": "USD"}, "wasPrice": {"price": 129.99}},
    "sellerDisplayName": "Walmart.com",
    "fulfillmentType": "FC",
    "averageRating": 4.3,
    "numberOfReviews": 11308,
    "imageInfo": {"allImages": [{"url": "https://i5.walmartimages.com/seo/airpods.jpeg"}]},
}
WALMART_REVIEWS = {
    "ratingValueOneCount": 10,
    "ratingValueTwoCount": 5,
    "ratingValueThreeCount": 5,
    "ratingValueFourCount": 20,
    "ratingValueFiveCount": 60,
    "customerReviews": [
        {
            "rating": 5,
            "reviewSubmissionTime": "5/30/2025",
            "reviewTitle": "Typical",
            "reviewText": "Work great.",
            "badges": [{"id": "VerifiedPurchaser"}],
        }
    ],
}


def test_walmart_page_data_wins_over_json_ld() -> None:
    url = "https://www.walmart.com/ip/Apple-AirPods-4/11381374703"
    facts, _ = parse_page(_walmart_page(WALMART_PRODUCT, WALMART_REVIEWS), url)
    assert facts.platform == "walmart"
    assert (facts.title, facts.brand) == ("Apple AirPods 4", "Apple")
    assert (facts.price, facts.list_price, facts.currency) == (89.0, 129.99, "USD")
    assert (facts.seller, facts.ships_from) == ("Walmart.com", "Walmart")
    assert (facts.rating, facts.rating_count) == (4.3, 11308)
    assert facts.rating_histogram == {1: 0.1, 2: 0.05, 3: 0.05, 4: 0.2, 5: 0.6}
    review = facts.reviews[0]
    assert (review.posted, review.verified, review.rating) == (date(2025, 5, 30), True, 5.0)
    assert facts.image_urls[0] == "https://i5.walmartimages.com/seo/airpods.jpeg"
    assert product_id(url, facts) == "walmart:11381374703"


def test_walmart_marketplace_seller_and_no_was_price() -> None:
    product = {**WALMART_PRODUCT, "sellerDisplayName": "Shenzhen Haoyu Trading", "fulfillmentType": "MARKETPLACE"}
    product["priceInfo"] = {"currentPrice": {"price": 15.94}, "wasPrice": None}
    facts, _ = parse_page(_walmart_page(product), "https://www.walmart.com/ip/-/11826501717")
    assert (facts.seller, facts.ships_from, facts.list_price) == ("Shenzhen Haoyu Trading", "", None)


async def test_walmart_is_a_marketplace_so_its_script_timers_never_count() -> None:
    facts, text = parse_page(_walmart_page(WALMART_PRODUCT), "https://www.walmart.com/ip/-/11381374703")
    assert facts.timers  # "global_evergreen" in a page script would read as a restarting countdown on a shop
    classified = await classify_node(state(facts, text or "Apple AirPods 4"))
    assert classified["page_type"] == "aggregate_listing"
    out = await DarkPatternsSkill().run(state(facts, page_type="aggregate_listing"))
    assert out["features"]["dark_patterns.timer"] == 0.0
    assert out["features"]["dark_patterns.timer_resets"] == 0.0


# ── Market ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("facts", "url", "market"),
    [
        (PageFacts(currency="USD"), "https://shop.example.com/p", "us"),
        (PageFacts(currency="USD"), "https://shop.example.de/p", "eu"),  # a German shop pricing in dollars
        (PageFacts(currency="USD", vat_ids=["NL123456789B01"]), "https://shop.example.com/p", "eu"),
        (PageFacts(currency="EUR"), "https://shop.example.com/p", "eu"),
        (PageFacts(), "https://shop.example.com/p", "eu"),  # no evidence: EU rules, as before
    ],
)
def test_shop_market_needs_positive_evidence_for_us(facts: PageFacts, url: str, market: str) -> None:
    assert shop_market(facts, url) == market


@pytest.mark.parametrize(
    ("text", "market"),
    [
        ("Sneakers $59.99, socks $9.99, free shipping over $50", "us"),
        ("Sneakers €59,99, sokken 9,99 €. Gratis verzending vanaf $50 (US store)", "eu"),
        ("Sneakers £59.99, socks $9.99", "eu"),  # a tie is not evidence
        ("Welcome to our store", "eu"),
    ],
)
def test_shop_market_reads_dollar_prices_when_facts_have_no_currency(text: str, market: str) -> None:
    assert shop_market(PageFacts(), "https://shop.example.com/", text) == market
    assert shop_market(PageFacts(), "https://shop.example.nl/", text) == "eu"  # a Dutch domain always is


# ── EU rules and US shops ─────────────────────────────────────────────────────


async def test_a_us_company_counts_as_the_imprint_of_a_us_shop() -> None:
    text = "© 2026 Acme Outdoor Goods LLC. All rights reserved."
    us = await EuRegistrySkill(vies=False).run(state(US, text))
    assert us["features"]["eu_registry.imprint"] == 1.0
    eu = await EuRegistrySkill(vies=False).run(state(EU, text))  # an EU shop still needs a real imprint
    assert eu["features"]["eu_registry.imprint"] == 0.0
    bare = await EuRegistrySkill(vies=False).run(state(US, "Great shoes."))
    assert bare["features"]["eu_registry.imprint.not_applicable"] == 1.0  # not required, so not missing


def test_scorer_reads_not_applicable_as_neutral() -> None:
    model = scoring.Model(
        version="t",
        features=["eu_registry.imprint", "listing.discount_pct"],
        means=[0.5, 0.0],
        scales=[0.5, 1.0],
        coefficients=[-1.0, 1.0],
        intercept=0.0,
        calibration=[],
    )
    absent = model.contributions({"listing.discount_pct": 0.0})
    na = model.contributions({"eu_registry.not_applicable": 1.0, "listing.discount_pct": 0.0})
    assert absent[0] > 0  # a missing imprint raises the risk on an EU shop
    assert na[0] == 0.0  # but not on a shop EU law doesn't cover
    full = scoring.score({"eu_registry.not_applicable": 1.0, "listing.discount_pct": 0.0}, model=model)
    assert full is not None and full.confidence == pytest.approx(abs(2 * full.risk_score - 1))  # fully measured


def test_a_us_shop_is_never_told_about_eu_withdrawal_law() -> None:
    features = {"eu_registry.withdrawal_right": 0.0, "eu_registry.imprint": 1.0}
    us = shopper.build(40, features=features, findings=[], facts=US, page_type="storefront", market="us")
    eu = shopper.build(40, features=features, findings=[], facts=EU, page_type="storefront")
    assert [r.type for r in us.reasons] == ["no_return_policy"]
    assert [r.type for r in eu.reasons] == ["no_withdrawal"]
    assert "EU" not in us.reasons[0].detail


# ── US identity ───────────────────────────────────────────────────────────────


async def test_us_identity_finds_company_address_and_phone() -> None:
    text = (
        "Contact us. Acme Outdoor Goods LLC, 1200 Main Street, Suite 400, Austin, TX 78701. Call (512) 555-0134. "
        "© 2026 Acme Outdoor Goods LLC."
    )
    out = await UsIdentitySkill().run(state(US, text))
    f = out["features"]
    assert f["us_identity.us_address"] == 1.0  # "Suite" is an office, not a mailbox
    assert f["us_identity.mailbox_only"] == 0.0
    assert f["us_identity.business_entity"] == 1.0
    assert f["us_identity.us_phone"] == 1.0
    evidence = {x.type: x.evidence for x in out["findings"]}
    assert "Austin, TX 78701" in evidence["us_address"]
    assert evidence["business_entity"] == "Company: Acme Outdoor Goods LLC"


async def test_us_identity_mailbox_only_and_nothing_found() -> None:
    out = await UsIdentitySkill().run(state(US, "Write to us: 2810 N Church St PMB 123, Wilmington, DE 19802."))
    assert out["features"]["us_identity.mailbox_only"] == 1.0  # the street part belongs to a mailbox address
    out = await UsIdentitySkill().run(state(US, "Mail: PO Box 55, Reno, NV 89501"))
    assert (out["features"]["us_identity.us_address"], out["features"]["us_identity.mailbox_only"]) == (0.0, 1.0)
    # Nothing on the page is not evidence of anything: every feature stays off.
    out = await UsIdentitySkill().run(state(US, "Great shoes for every season."))
    assert not any(out["features"].values())
    assert out["findings"] == []


async def test_us_identity_only_runs_on_us_shops() -> None:
    out = await UsIdentitySkill().run(state(EU, "Acme LLC, 1200 Main Street, Austin, TX 78701"))
    assert out["features"] == {"us_identity.not_applicable": 1.0}


@pytest.mark.parametrize(
    ("text", "entity"),
    [
        # The copyright line of a footer, in the forms shops write it: the name, never the year, © or "Copyright".
        ("© 2026 Acme Outdoor LLC. All rights reserved.", "Acme Outdoor LLC"),
        ("Copyright © 2024-2026 Acme Goods, Inc.", "Acme Goods, Inc."),
        ("©2025 ACME LLC", "ACME LLC"),
        ("Copyright 2026, Blue Ridge Supply Co. LLC", "Blue Ridge Supply Co. LLC"),
        ("COPYRIGHT © 2026 SMITH & SONS LLC", "SMITH & SONS LLC"),
        ("© 2026 Smith & Sons LLC", "Smith & Sons LLC"),
        ("Copyright Acme Outdoor LLC", "Acme Outdoor LLC"),
        ("(c) 2026 Acme Outdoor L.L.C.", "Acme Outdoor L.L.C."),
        ("© 2020 5econds Pte. Ltd.", "5econds Pte. Ltd."),
        # The page text puts a line break around every inline tag: "©<span>2026</span>, <a>Acme <b>Outdoor</b></a>".
        ("©\n2026\n, \nAcme \nOutdoor\n \nLLC", "Acme Outdoor LLC"),
        # The shop's own name wins over the platform and payment companies a footer credits, wherever they stand.
        ("Powered by Shopify Inc. © 2026 Acme Outdoor LLC", "Acme Outdoor LLC"),
        ("© 2026 Acme Outdoor LLC. Payments are processed by Square, Inc.", "Acme Outdoor LLC"),
        # Outside a copyright line: no menu items, sentence words or labels in front of the name.
        ("Home\nShop\nAbout Us\nAcme Outdoor LLC", "Acme Outdoor LLC"),
        ("Sold By Acme Outdoor LLC", "Acme Outdoor LLC"),
        ("Manufacturer\nKao USA Inc.", "Kao USA Inc."),
        ("The Acme Company LLC, 1200 Main Street", "The Acme Company LLC"),
    ],
)
def test_find_identity_names_the_company(text: str, entity: str) -> None:
    assert find_identity(text).entity == entity


@pytest.mark.parametrize(
    "text",
    [
        "© 2026 Shopify",  # a copyright line without a company form
        "© 2026, Acme Outdoor. Powered by Shopify Inc.",  # the platform is not the shop
        "Powered by Shopify Inc.",
        "Built with Wix.com Ltd.",
        "Payments by Square, Inc. and PayPal, Inc.",
        "Google Analytics\n\nAnbieter\n\nGoogle LLC",
        "Verarbeitende Firma: Google Ireland Ltd, Gordon House",  # a cookie notice names the service's subsidiary
        "© 2026 LLC",  # a year is no name
        "Website by Pixel Studio LLC",  # the web agency, not the shop
        "We are not an LLC.",
        "Note: We Are Not An LLC.",
        "Why Choose An LLC For Your Business",
        "Our Company LLC",
        "Instagram\nTikTok\nInc",
    ],
)
def test_find_identity_ignores_what_is_not_the_shop(text: str) -> None:
    assert find_identity(text).entity == ""


FOOTER_PAGE = """
<html><body><main><h1>Trail Jacket</h1><p>$120.00</p></main>
<footer><nav><a href="/">Home</a><a href="/pages/about">About Us</a></nav>
<div class="copyright">©<span>2026</span>, <a href="/">Acme <b>Outdoor</b></a> <span>LLC</span>. All rights reserved.
<a href="https://www.shopify.com">Powered by Shopify</a></div></footer></body></html>
"""


async def test_the_copyright_line_in_a_footer_names_the_company() -> None:
    # The page text the LLM sees has no footer, so the copyright line has to reach the skill through the facts.
    facts, text = parse_page(FOOTER_PAGE, "https://acme-outdoor.example.com/products/trail-jacket")
    assert "Acme" not in text
    out = await UsIdentitySkill().run(state(facts, text))
    assert out["features"]["us_identity.business_entity"] == 1.0
    assert {x.type: x.evidence for x in out["findings"]}["business_entity"] == "Company: Acme Outdoor LLC"
    imprint = await EuRegistrySkill(vies=False).run(state(facts, text))
    assert imprint["features"]["eu_registry.imprint"] == 1.0


# ── Shipping and return terms ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "days"),
    [
        ("Processing 1-3 days. Shipping takes 7-20 business days.", 20),
        ("Delivery: 2–4 weeks.", 28),
        ("Free shipping, delivered in 3-5 days.", 5),
        ("30-day returns, 14-day withdrawal right.", 0),  # no ranges about delivery
        ("Returns accepted 15-30 days after delivery.", 0),  # a range next to "returns" is never delivery
        ("Our 10-25 years of experience.", 0),  # no shipping words nearby
    ],
)
def test_longest_delivery_days(text: str, days: int) -> None:
    assert longest_delivery_days(text) == days


async def test_dropshipping_return_terms() -> None:
    text = (
        "Shipping: orders arrive in 10-25 business days. Returns must be sent to our warehouse in Shenzhen. "
        "Please contact us to get the return address. A 25% restocking fee applies. Proudly made in the USA."
    )
    out = await ShopPoliciesSkill().run(state(US, text))
    f = out["features"]
    assert f["shop_policies.long_delivery"] == 1.0
    assert f["shop_policies.returns_abroad"] == 1.0
    assert f["shop_policies.return_address_withheld"] == 1.0
    assert f["shop_policies.restocking_fee_high"] == 1.0
    assert f["shop_policies.origin_doubt"] == 1.0
    types = {x.type for x in out["findings"]}
    assert {"long_delivery", "returns_abroad", "return_address_withheld", "restocking_fee", "origin_doubt"} <= types


async def test_ordinary_us_return_terms_are_not_flagged() -> None:
    text = (
        "Free 2-day shipping. 30-day returns: print a prepaid label and send it to our Columbus, OH warehouse. "
        "A 15% restocking fee applies to opened electronics. Made in the USA in Portland, Oregon. "
        "We also ship to customers in Hong Kong and China."
    )
    out = await ShopPoliciesSkill().run(state(US, text))
    assert not any(out["features"].values())
    assert out["findings"] == []


# ── Drip pricing with US sales tax ────────────────────────────────────────────


def test_checkout_tax_line() -> None:
    assert checkout_tax("<p>Subtotal $20.00</p><p>Estimated tax $1.65</p><p>Total $21.65</p>") == 1.65
    assert checkout_tax("<p>Total $21.65 (taxes included)</p>") is None


async def test_us_sales_tax_at_checkout_is_not_drip_pricing() -> None:
    checkout = "<p>Subtotal $20.00</p><p>Shipping FREE</p><p>Sales tax $1.70</p><p>Order total $21.70</p>"
    us = await DarkPatternsSkill().run(state(US, checkout_content=checkout))
    assert us["features"]["dark_patterns.checkout_markup_pct"] == 0.0
    fees = "<p>Subtotal $20.00</p><p>Service fee $4.00</p><p>Sales tax $1.70</p><p>Order total $25.70</p>"
    added = await DarkPatternsSkill().run(state(US, checkout_content=fees))
    assert added["features"]["dark_patterns.checkout_markup_pct"] == pytest.approx(0.2)


def test_sales_tax_wording_is_not_a_late_fee() -> None:
    facts, _ = parse_page("<html><body><p>$20.00. Sales tax calculated at checkout.</p></body></html>")
    assert facts.fee_mentions == []


# ── Shopper copy ──────────────────────────────────────────────────────────────


def test_us_reasons_and_positive_render() -> None:
    features = {
        "shop_policies.returns_abroad": 1.0,
        "shop_policies.restocking_fee_high": 1.0,
        "shop_policies.long_delivery": 1.0,
        "us_identity.us_address": 1.0,
    }
    findings = [
        Finding(skill="shop_policies", type="returns_abroad", evidence="Returns go to Shenzhen."),
        Finding(skill="shop_policies", type="restocking_fee", evidence="Returns cost a 25% restocking fee."),
        Finding(skill="shop_policies", type="long_delivery", evidence="Delivery can take up to 25 days."),
        Finding(skill="us_identity", type="business_entity", evidence="Company: Acme Outdoor Goods LLC"),
    ]
    verdict = shopper.build(40, features=features, findings=findings, facts=US, page_type="storefront")
    reasons = {r.type: r.detail for r in verdict.reasons}
    assert "Shenzhen" in reasons["returns_abroad"]
    assert "25%" in reasons["restocking_fee"]
    assert "25" in reasons["long_delivery"]
    assert any("Acme Outdoor Goods LLC" in p.detail for p in verdict.positives)
