"""
The Unfret test shop (``/test-shop``): served like the site, and every trick on it caught by the quick and deep tiers.

The pinned signals are what each trick raises in the skills (their findings and features), and the verdict both
tiers give. A trick that stops being caught fails here by name; a retrained scorer that moves the band needs a look,
not a wider assert. What is knowingly *not* caught is pinned too, so a fix shows up as a failing "gap" test.
"""

import re
from collections.abc import AsyncIterator
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from bs4 import BeautifulSoup, Tag
from langchain_core.embeddings import DeterministicFakeEmbedding
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

from unfret import llm, scoring
from unfret.config import settings
from unfret.graph import SKILLS, build_graph, nodes, triage
from unfret.graph.skills import content, reviews
from unfret.main import app
from unfret.routes.pages import PAGE_HEADERS, STATIC_DIR, asset_tag
from unfret.routes.test_shop import render
from unfret.test_shop import is_test_shop
from unfret.x402.verdict import skills_for, to_verdict

# Captured at import, before the suite's autouse fixture hides the shipped models.
_LOAD_MODEL = scoring.load_model

URL = "https://unfret.net/test-shop"
BANNER = "Unfret test shop — every trick here is copied from real listings, names removed"
PAGES = ("/test-shop", "/test-shop/checkout")

# Each trick and the findings (skill, type) it must raise in both tiers.
TRICKS: dict[str, set[tuple[str, str]]] = {
    "countdown that resets on reload": {("dark_patterns", "timer")},
    "someone-just-bought pop-ups": {("html_source_signals", "timer_app")},
    "closing-down story": {("pricing", "permanent_sale")},
    "'was' price never charged": {("pricing", "implausible_discount"), ("pricing", "reference_anchor")},
    "foreign shipping in the small print": {
        ("shop_policies", "long_delivery"),
        ("shop_policies", "returns_abroad"),
        ("shop_policies", "return_address_withheld"),
        ("shop_policies", "restocking_fee"),
        ("shop_policies", "origin_doubt"),
    },
    "fees in the small print": {("dark_patterns", "fee")},
    "pre-ticked paid add-on": {("dark_patterns", "preticked_addon")},
    "dropshipping-app markers": {("html_source_signals", "dropship_app"), ("html_source_signals", "supplier_trace")},
    "review-import app": {("html_source_signals", "review_import_app")},
    "hidden text aimed at AI": {("injection", "hidden_instruction")},
    "made-up brand, keyword-stuffed title": {("listing", "brand_name"), ("listing", "title")},
}
# Tricks only the deep tier reads: the reviews, and the checkout page when the caller sends it.
DEEP_TRICKS: dict[str, set[tuple[str, str]]] = {
    "invented reviews": {("reviews", "review_velocity"), ("reviews", "rating_shape"), ("reviews", "duplicates")},
}
# The verdict: the band, and its three strongest reasons (identifiers, not the words a shopper reads).
QUICK_VERDICT = ("junk_or_scam", ["timer_resets", "preticked_addon", "implausible_discount"])
DEEP_VERDICT = ("junk_or_scam", ["timer_resets", "preticked_addon", "implausible_discount"])
DEEP_CHECKOUT_VERDICT = ("junk_or_scam", ["timer_resets", "preticked_addon", "drip_pricing"])


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


# ── The address ──────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://unfret.net/test-shop", True),
        ("https://unfret.net/test-shop/", True),
        ("https://www.unfret.net/test-shop?variant=1", True),
        ("https://dev.unfret.net/test-shop/checkout", True),
        ("dev.unfret.net/test-shop", True),
        ("https://unfret.net/test-shopping", False),
        ("https://unfret.net/products/test-shop", False),
        ("https://unfret.net/", False),
        ("https://evil-unfret.net/test-shop", False),
        ("https://unfret.net.evil.example/test-shop", False),
        ("https://shop.example/test-shop", False),
        ("", False),
    ],
)
def test_the_test_shop_is_told_by_its_address(url: str, expected: bool) -> None:
    assert is_test_shop(url) is expected


def test_this_service_is_a_test_shop_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "public_base_url", "https://staging.example")
    assert is_test_shop("https://staging.example/test-shop")
    assert not is_test_shop("https://other.example/test-shop")


# ── The pages ────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", PAGES)
async def test_pages_are_served_like_the_site_and_kept_out_of_search(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-security-policy"] == PAGE_HEADERS["Content-Security-Policy"]
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    soup = BeautifulSoup(response.text, "html.parser")
    robots = soup.find("meta", attrs={"name": "robots"})
    assert isinstance(robots, Tag) and "noindex" in str(robots["content"])
    banner = soup.find(class_="ts-banner")
    assert isinstance(banner, Tag) and " ".join(banner.get_text().split()) == BANNER
    # Only the banner ties it to Unfret: the shop never names Unfret or uses the site's look.
    assert response.text.count("Unfret") == 1
    assert "site.css" not in response.text and "figtree" not in response.text.lower()


@pytest.mark.parametrize("path", PAGES)
async def test_pages_run_no_inline_code_and_load_nothing_from_elsewhere(client: httpx.AsyncClient, path: str) -> None:
    html = (await client.get(path)).text
    soup = BeautifulSoup(html, "html.parser")

    for script in soup.find_all("script"):
        if script.get("src"):
            assert str(script["src"]).startswith(f"/static/{asset_tag()}/test-shop/"), script
        else:  # data blocks only: the CSP would refuse inline code anyway
            assert script.get("type") in {"application/json", "application/ld+json"}, script
    assert not soup.find_all("style") and not soup.find_all(style=True), "inline styles are refused by the CSP"
    assert not [t for t in soup.find_all(True) if any(a.startswith("on") for a in t.attrs)], "inline handlers"
    tags = [*soup.find_all("script", src=True), *soup.find_all("img"), *soup.find_all("link")]
    loaded = [(t.name, str(t.get("src") or t.get("href"))) for t in tags]
    loaded += [("og", str(m["content"])) for m in soup.find_all("meta", property="og:image")]
    for name, address in loaded:
        if name == "link" and not address.startswith("/static/"):
            continue
        assert address.startswith(f"/static/{asset_tag()}/test-shop/"), (name, address)
        assert (STATIC_DIR / address.removeprefix(f"/static/{asset_tag()}/")).is_file(), address
    for link in soup.find_all("a", href=True):  # every link stays in the shop (nothing to follow elsewhere)
        href = urlsplit(str(link["href"]))
        assert not href.scheme and not href.netloc and (not href.path or href.path.startswith("/test-shop")), link


async def test_the_shop_static_files_are_served(client: httpx.AsyncClient) -> None:
    for name in ("shop.css", "shop.js", "favicon.svg", "projector-hero.jpg"):
        response = await client.get(f"/static/{asset_tag()}/test-shop/{name}")
        assert response.status_code == 200, name


def test_reviews_are_dated_from_the_day_the_page_is_served() -> None:
    html = render("product.html", today=date(2027, 3, 2))

    assert "{{" not in re.sub(r"\$\{\{amount\}\}", "", html)  # every date filled in (Shopify's money format stays)
    assert '<meta itemprop="datePublished" content="2027-03-01">' in html
    assert "<time>Feb 11, 2027</time>" in html  # 19 days before
    assert "Mar 1, 2027" in html


# ── The checks ───────────────────────────────────────────────────────────────────────────────────────────────────


def _findings(result: dict[str, Any]) -> set[tuple[str, str]]:
    return {(f.skill, f.type) for f in result["findings"]}


def _verdict(result: dict[str, Any], tier: str) -> tuple[str | None, list[str]]:
    verdict = to_verdict(result, policy="balanced", tier=tier)  # type: ignore[arg-type]
    return verdict.band, [f.type for f in verdict.flags]


@pytest.fixture
def local_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """No outside lookups (RDAP, Wikidata, VIES) and no stores: the page alone."""
    monkeypatch.setattr(settings, "network_lookups", False)
    monkeypatch.setattr(settings, "fingerprints", False)


@pytest.fixture
async def quick(monkeypatch: pytest.MonkeyPatch, local_only: None) -> dict[str, Any]:
    model = _LOAD_MODEL.__wrapped__(scoring.MODEL_QUICK_PATH)
    assert model is not None, "the quick model ships in the repo"
    monkeypatch.setattr(scoring, "load_model", lambda path=scoring.MODEL_PATH: model)
    monkeypatch.setattr(llm, "get_llm", lambda: pytest.fail("the quick tier calls no LLM"))
    graph = build_graph(skills_for("quick", SKILLS), model_path=scoring.MODEL_QUICK_PATH, llm=False)
    result: dict[str, Any] = await graph.ainvoke(
        {"page_content": render("product.html"), "url": URL, "flags": [], "explain": False}
    )
    return result


# Canned answers for the LLM calls, in line with how the real model reads this page, and the headline.
ANSWERS: dict[type[BaseModel], BaseModel] = {
    content._Signals: content._Signals(
        scam_copy=0.9,
        generic_product=0.8,
        title_word_soup=0.9,
        generic_brand=0.9,
        brand_recognized=False,
        implausible_claims=0.8,
        implausible_claim_examples=["Clinically proven to help you fall asleep 87% faster"],
        missing_trust_info=0.8,
        addresses_the_reader_ai=True,
        evidence=["We're closing down: everything must go!", "Hurry! Only 3 left in stock."],
    ),
    triage._TriageAnswer: triage._TriageAnswer(run_reverse_image=True, reason="A generic gadget."),
    nodes._Explanation: nodes._Explanation(explanation="A fake countdown and a paid extra ticked for you."),
}


class FakeChatModel:
    """Every structured LLM call gets its canned answer; the page is classified by rules, never by the LLM."""

    def with_structured_output(self, schema: type[BaseModel], **_: Any) -> RunnableLambda:
        return RunnableLambda(lambda _: ANSWERS[schema])


async def _deep(monkeypatch: pytest.MonkeyPatch, *, checkout: bool = False) -> dict[str, Any]:
    model = _LOAD_MODEL.__wrapped__(scoring.MODEL_PATH)
    assert model is not None, "the model ships in the repo"
    monkeypatch.setattr(scoring, "load_model", lambda path=scoring.MODEL_PATH: model)
    monkeypatch.setattr(llm, "get_llm", FakeChatModel)
    monkeypatch.setattr(reviews, "get_embeddings", lambda: DeterministicFakeEmbedding(size=16))
    state: dict[str, Any] = {"page_content": render("product.html"), "url": URL, "flags": [], "explain": True}
    if checkout:
        state["checkout_content"] = render("checkout.html")
    result: dict[str, Any] = await build_graph(SKILLS).ainvoke(state)
    return result


@pytest.fixture
async def deep(monkeypatch: pytest.MonkeyPatch, local_only: None) -> dict[str, Any]:
    return await _deep(monkeypatch)


@pytest.mark.parametrize("trick", TRICKS)
def test_the_quick_tier_catches_every_trick_on_the_page(quick: dict[str, Any], trick: str) -> None:
    assert quick["page_type"] == "storefront"
    missing = TRICKS[trick] - _findings(quick)
    assert not missing, f"quick tier misses {trick!r}: {missing}"


@pytest.mark.parametrize("trick", [*TRICKS, *DEEP_TRICKS])
def test_the_deep_tier_catches_every_trick_on_the_page(deep: dict[str, Any], trick: str) -> None:
    assert deep["page_type"] == "storefront"
    missing = (TRICKS | DEEP_TRICKS)[trick] - _findings(deep)
    assert not missing, f"deep tier misses {trick!r}: {missing}"


def test_the_countdown_is_caught_as_one_that_restarts(quick: dict[str, Any]) -> None:
    assert quick["features"]["dark_patterns.timer_resets"] == 1.0
    timer = next(f for f in quick["findings"] if f.type == "timer")
    assert timer.evidence.startswith("A countdown timer that restarts")


def test_the_invented_reviews_are_a_burst_of_repeats(deep: dict[str, Any]) -> None:
    features = deep["features"]
    assert features["reviews.burst_share"] == 1.0  # every review in one month, 2,847 ratings
    assert features["reviews.duplicate_share"] >= 0.15
    assert features["reviews.j_shaped"] == 1.0
    assert features["reviews.short_share"] > 0  # "Wow", "Love it"


def test_the_quick_verdict(quick: dict[str, Any]) -> None:
    assert _verdict(quick, "quick") == QUICK_VERDICT


def test_the_deep_verdict(deep: dict[str, Any]) -> None:
    assert _verdict(deep, "deep") == DEEP_VERDICT
    assert deep["verdict"]["headline"] == "A fake countdown and a paid extra ticked for you."


@pytest.mark.usefixtures("local_only")
async def test_the_checkout_page_shows_fees_added_at_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await _deep(monkeypatch, checkout=True)
    drip = next(f for f in result["findings"] if f.type == "drip_pricing")
    assert drip.evidence == "Listed at 39.99, checkout total 61.88 (+55%)."
    assert _verdict(result, "deep") == DEEP_CHECKOUT_VERDICT


# ── Known gaps (input for the scorer, pinned so a fix shows up here) ─────────────────────────────────────────────


def test_gap_the_was_price_is_never_checked_against_a_price_history(quick: dict[str, Any]) -> None:
    # "Was never charged" needs a month of the product's prices, and the test shop is kept out of the price store:
    # the fake "was" price is caught as an implausible discount with a reference anchor instead.
    assert "pricing.was_above_30d_low" not in quick["features"]


def test_gap_a_us_shop_without_any_company_details_is_not_marked_down(
    quick: dict[str, Any], deep: dict[str, Any]
) -> None:
    # No US law requires an imprint, so its absence is not applicable; only the deep tier's content read notices
    # that the shop names no company, address or phone ("Little shop information").
    assert quick["features"]["eu_registry.imprint.not_applicable"] == 1.0
    assert deep["features"]["content.missing_trust_info"] >= 0.6
