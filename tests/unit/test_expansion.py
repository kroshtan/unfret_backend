"""The page classes beyond products: classification, one subgraph per class, class headlines, retries."""

from typing import Any, ClassVar

import httpx
import pytest
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

from unfret import shopper
from unfret.config import settings
from unfret.facts import PageFacts, parse_page
from unfret.facts.schema import SellerProfile
from unfret.graph import build_graph, nodes, transient
from unfret.graph.skills import ANALYSED_PAGES, P2P_PAGES, PRODUCT_PAGES, SERVICE_SITES, Skill, SkillOutput
from unfret.graph.skills.content import ContentSkill
from unfret.graph.skills.domain_age import DomainAgeSkill
from unfret.graph.skills.injection import InjectionSkill
from unfret.graph.skills.listing import ListingSkill
from unfret.graph.state import AnalysisState, PageType

PAGE = "<html><body><h1>Fiets te koop</h1><p>Mooie fiets, ophalen in Utrecht.</p></body></html>"


class _Skill(Skill):
    def __init__(self, name: str, page_types: frozenset[PageType] = ANALYSED_PAGES) -> None:
        self.name = name
        self.description = name
        self.page_types = page_types  # type: ignore[misc]
        self.calls = 0

    async def run(self, state: AnalysisState) -> SkillOutput:
        self.calls += 1
        return self.output({"ok": True}, note="ok")


class _LLM:
    """Answers each structured-output call with a canned instance of its schema, and records the schemas asked."""

    def __init__(self) -> None:
        self.outputs: dict[type[BaseModel], BaseModel] = {}
        self.asked: list[type[BaseModel]] = []

    def structured(self, schema: type[BaseModel]) -> RunnableLambda:
        def respond(messages: Any) -> BaseModel:
            self.asked.append(schema)
            return self.outputs[schema]

        return RunnableLambda(respond)


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> _LLM:
    fake = _LLM()
    monkeypatch.setattr(nodes, "get_structured_llm", fake.structured)
    fake.outputs[nodes._JudgeOutput] = nodes._JudgeOutput(score=70, explanation="Fine.")
    return fake


@pytest.fixture
def expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "expansion_classes", True)


@pytest.fixture
def no_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "expansion_classes", False)


def _state(url: str, facts: PageFacts | None = None, page: str = PAGE) -> dict[str, Any]:
    state: dict[str, Any] = {"page_content": page, "url": url, "flags": []}
    if facts is not None:
        state["page_facts"] = facts
    return state


AD_URL = "https://www.marktplaats.nl/v/fietsen-en-brommers/fietsen-dames/m2187654321-gazelle-damesfiets"


# ── Classification ──────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("no_expansion")
async def test_without_the_flag_a_marketplace_ad_is_a_product_page_as_before(llm: _LLM) -> None:
    facts = PageFacts(platform="marktplaats", url=AD_URL, title="Gazelle damesfiets", price=150.0)

    result = await build_graph([_Skill("a")]).ainvoke(_state(AD_URL, facts))

    assert result["page_type"] == "storefront"
    assert nodes._ClassifyOutputExpanded not in llm.asked


@pytest.mark.usefixtures("no_expansion")
async def test_without_the_flag_the_classifier_prompt_is_unchanged(llm: _LLM) -> None:
    llm.outputs[nodes._ClassifyOutput] = nodes._ClassifyOutput(page_type="non_product", platform="", reasoning="r")

    result = await build_graph([_Skill("a")]).ainvoke(_state(""))

    assert result["score"] == -1
    assert llm.asked == [nodes._ClassifyOutput]


@pytest.mark.usefixtures("expansion")
@pytest.mark.parametrize(
    ("platform", "url", "page_type"),
    [
        ("marktplaats", AD_URL, "p2p_listing"),
        ("2dehands", "https://www.2dehands.be/v/fietsen/m1234567890-stadsfiets", "p2p_listing"),
        ("vinted", "https://www.vinted.nl/items/4567890123-jas", "p2p_listing"),
        ("craigslist", "https://sfbay.craigslist.org/sfc/bik/d/san-francisco-bike/7712345678.html", "p2p_listing"),
        ("facebook", "https://www.facebook.com/marketplace/item/123456789012345/", "p2p_listing"),
        ("ebay", "https://www.ebay.de/itm/123456789012", "p2p_listing"),
        ("werkspot", "https://www.werkspot.nl/profiel/loodgieter-jansen", "service_listing"),
        (
            "thumbtack",
            "https://www.thumbtack.com/ca/san-francisco/plumbers/acme-plumbing/service/123456",
            "service_listing",
        ),
        (
            "angi",
            "https://www.angi.com/companylist/us/ca/san-francisco/acme-plumbing-reviews-123.htm",
            "service_listing",
        ),
        ("homeadvisor", "https://www.homeadvisor.com/rated.AcmePlumbing.123456.html", "service_listing"),
    ],
)
async def test_known_platforms_are_classified_without_the_llm(
    llm: _LLM, platform: str, url: str, page_type: str
) -> None:
    facts = PageFacts(platform=platform, url=url, title="Something", seller="Jan de Vries", price=10.0)  # type: ignore[arg-type]

    result = await build_graph([_Skill("a")]).ainvoke(_state(url, facts))

    assert result["page_type"] == page_type
    assert not any(s in llm.asked for s in (nodes._ClassifyOutput, nodes._ClassifyOutputExpanded))
    if page_type == "p2p_listing":
        assert result["facts"].seller == ""  # a private seller's name is never kept


@pytest.mark.usefixtures("expansion")
@pytest.mark.parametrize(
    ("text", "page_type"),
    [
        ("Business seller · Registered as a business", "aggregate_listing"),
        ("Privater Verkäufer", "p2p_listing"),
        ("Nothing about the seller", "p2p_listing"),
    ],
)
async def test_ebay_business_sellers_are_marketplace_listings(llm: _LLM, text: str, page_type: str) -> None:
    url = "https://www.ebay.com/itm/123456789012"
    page = f"<html><body><h1>Drill</h1><p>{text}</p></body></html>"
    facts = PageFacts(platform="ebay", url=url, title="Drill", price=40.0)

    result = await build_graph([_Skill("a")]).ainvoke(_state(url, facts, page))

    assert result["page_type"] == page_type


@pytest.mark.usefixtures("expansion")
async def test_a_platform_label_from_the_extractor_decides_ebay() -> None:
    facts = PageFacts(platform="ebay", title="Drill", seller_profile=SellerProfile(seller_type="business"))
    state: Any = {"cleaned_content": "Private seller", "url": "https://www.ebay.com/itm/1", "facts": facts}

    result = await nodes.classify_node(state)

    assert result["page_type"] == "aggregate_listing"


@pytest.mark.usefixtures("expansion")
async def test_a_platform_search_page_goes_to_the_llm_not_the_product_rules(llm: _LLM) -> None:
    url = "https://www.marktplaats.nl/l/fietsen-en-brommers/"
    llm.outputs[nodes._ClassifyOutputExpanded] = nodes._ClassifyOutputExpanded(
        page_type="non_product", platform="Marktplaats", reasoning="Search results."
    )
    facts = PageFacts(platform="marktplaats", url=url, title="Fietsen", price=99.0)

    result = await build_graph([_Skill("a")]).ainvoke(_state(url, facts))

    assert result["score"] == -1
    assert llm.asked == [nodes._ClassifyOutputExpanded]


@pytest.mark.usefixtures("expansion")
async def test_product_pages_are_classified_as_before(llm: _LLM) -> None:
    amazon = PageFacts(platform="amazon", title="Lamp", price=20.0)
    shop = PageFacts(title="Lamp", price=20.0)

    on_amazon = await build_graph([_Skill("a")]).ainvoke(_state("https://www.amazon.de/dp/B0TEST1234", amazon))
    own_shop = await build_graph([_Skill("a")]).ainvoke(_state("https://lamp.example/p/1", shop))

    assert (on_amazon["page_type"], own_shop["page_type"]) == ("aggregate_listing", "storefront")


@pytest.mark.usefixtures("expansion")
async def test_service_company_structured_data_is_a_service_site(llm: _LLM) -> None:
    facts = PageFacts(title="Loodgieter Utrecht", organization_ld=[{"@type": "Plumber", "name": "Snel BV"}])

    result = await build_graph([_Skill("a")]).ainvoke(_state("https://snel.example/", facts))

    assert result["page_type"] == "service_site"


@pytest.mark.usefixtures("expansion")
@pytest.mark.parametrize("product_answer", ["aggregate_listing", "storefront"])
async def test_the_product_classification_decides_product_pages_as_without_the_flag(
    llm: _LLM, product_answer: str
) -> None:
    llm.outputs[nodes._ClassifyOutput] = nodes._ClassifyOutput(
        page_type=product_answer,  # type: ignore[arg-type]
        platform="",
        reasoning="r",
    )
    page = "<html><body><h1>Lamps</h1><p>Our collection. Add to cart.</p></body></html>"

    result = await build_graph([_Skill("a")]).ainvoke(_state("https://lamps.example/", page=page))

    assert result["page_type"] == product_answer
    assert llm.asked[0] is nodes._ClassifyOutput  # the same prompt as with the flag off
    assert nodes._ClassifyOutputExpanded not in llm.asked and nodes._NewClassOutput not in llm.asked


@pytest.mark.usefixtures("expansion")
@pytest.mark.parametrize(
    ("page", "answer", "page_type"),
    [
        ("<h1>Loodgieter Utrecht</h1><p>Lekkage? Wij komen snel.</p>", "service_site", "service_site"),
        ("<h1>Profiel: Jansen Klussen</h1><p>12 klussen gedaan.</p>", "service_listing", "service_listing"),
        ("<h1>Get your money back</h1><p>We help you recover funds.</p>", "service_site", "non_product"),  # no trade
        ("<h1>Cheap flights</h1><p>Book now.</p>", "none", "non_product"),
    ],
)
async def test_a_page_the_product_step_would_skip_is_asked_about_the_service_classes(
    llm: _LLM, page: str, answer: str, page_type: str
) -> None:
    llm.outputs[nodes._ClassifyOutput] = nodes._ClassifyOutput(page_type="non_product", platform="", reasoning="r")
    llm.outputs[nodes._NewClassOutput] = nodes._NewClassOutput(page_type=answer, reasoning="r")  # type: ignore[arg-type]

    result = await build_graph([_Skill("a")]).ainvoke(_state("https://unknown.example/", page=f"<body>{page}</body>"))

    assert result["page_type"] == page_type
    assert llm.asked[:2] == [nodes._ClassifyOutput, nodes._NewClassOutput]


@pytest.mark.usefixtures("expansion")
async def test_a_shop_without_anything_to_buy_that_names_a_trade_may_be_a_service_site(llm: _LLM) -> None:
    llm.outputs[nodes._ClassifyOutput] = nodes._ClassifyOutput(page_type="storefront", platform="", reasoning="r")
    llm.outputs[nodes._NewClassOutput] = nodes._NewClassOutput(page_type="service_site", reasoning="r")
    plumber = PageFacts(title="Loodgieter Amersfoort | Snel ter plaatse")
    shop = PageFacts(title="Loodgieter Amersfoort | Snel ter plaatse", price=19.95)

    as_service = await build_graph([_Skill("a")]).ainvoke(_state("https://lekkage.example/", plumber))
    as_shop = await build_graph([_Skill("a")]).ainvoke(_state("https://lekkage.example/", shop))

    assert (as_service["page_type"], as_shop["page_type"]) == ("service_site", "storefront")


@pytest.mark.usefixtures("expansion")
@pytest.mark.parametrize("answer", ["p2p_listing", "service_listing"])
async def test_an_ad_or_a_profile_only_on_a_platform_of_that_kind(llm: _LLM, answer: str) -> None:
    # A shop the LLM took for "a private seller on eBay" is no ad; only the platform table makes one.
    llm.outputs[nodes._ClassifyOutputExpanded] = nodes._ClassifyOutputExpanded(
        page_type=answer,  # type: ignore[arg-type]
        platform="eBay",
        reasoning="r",
    )
    facts = PageFacts(platform="vinted", url="https://www.vinted.nl/catalog", title="Jassen")

    result = await build_graph([_Skill("a")]).ainvoke(_state(facts.url, facts))

    expected = "p2p_listing" if answer == "p2p_listing" else "non_product"
    assert result["page_type"] == expected


@pytest.mark.usefixtures("expansion")
async def test_the_quick_tier_stays_product_only() -> None:
    facts = PageFacts(platform="marktplaats", url=AD_URL, title="Fiets", price=150.0)
    state: Any = {"cleaned_content": "Fiets", "url": AD_URL, "facts": facts}

    result = await nodes.classify_by_rules_node(state)

    assert result["page_type"] == "storefront"


# ── Subgraphs ───────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("expansion")
async def test_each_class_runs_only_its_skills(llm: _LLM) -> None:
    product, p2p, service_site, everywhere = (
        _Skill("product", PRODUCT_PAGES),
        _Skill("p2p", P2P_PAGES),
        _Skill("service_site", SERVICE_SITES),
        _Skill("everywhere"),
    )
    graph = build_graph([product, p2p, service_site, everywhere])
    facts = PageFacts(platform="marktplaats", url=AD_URL, title="Fiets", price=150.0)

    result = await graph.ainvoke(_state(AD_URL, facts))

    assert result["page_type"] == "p2p_listing"
    assert (product.calls, p2p.calls, service_site.calls, everywhere.calls) == (0, 1, 0, 1)
    assert result["features"] == {"p2p.ok": 1.0, "everywhere.ok": 1.0}
    # The classification is in the results once (the subgraph hands back only what it added).
    assert [r.split("\n")[0] for r in result["skill_results"]].count("[classify]") == 1


@pytest.mark.usefixtures("expansion")
async def test_every_class_streams_its_steps(llm: _LLM) -> None:
    graph = build_graph([_Skill("a")])
    facts = PageFacts(platform="werkspot", url="https://www.werkspot.nl/profiel/jansen", title="Jansen")

    chunks = [c async for c in graph.astream(_state(facts.url, facts), stream_mode="updates", subgraphs=True)]

    names = [node for _, update in chunks for node in update]
    assert names[:2] == ["preprocess", "classify"]
    assert {"a", "triage", "judge", "service"} <= set(names)


def test_existing_skills_declare_the_classes_they_clearly_apply_to() -> None:
    assert {"p2p_listing", "service_listing", "service_site"} <= ContentSkill.page_types
    assert {"p2p_listing", "service_listing", "service_site"} <= InjectionSkill.page_types
    assert "service_site" in DomainAgeSkill.page_types
    assert "p2p_listing" not in DomainAgeSkill.page_types
    assert ListingSkill.page_types == PRODUCT_PAGES


# ── Class headlines ───────────────────────────────────────────────────────────────────────────────────────────


def test_class_headlines_fit_the_headline_limit() -> None:
    for per_band in shopper.catalogue()["class_headlines"].values():
        for headline in per_band.values():
            assert shopper.fit_headline(headline) == headline


# ── Retries ─────────────────────────────────────────────────────────────────────────────────────────────────


class _Flaky(_Skill):
    retry: ClassVar[bool] = True

    def __init__(self, name: str, failures: int, error: Exception) -> None:
        super().__init__(name, PRODUCT_PAGES)
        self.failures = failures
        self.error = error

    async def run(self, state: AnalysisState) -> SkillOutput:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return self.output({"ok": True}, note="ok")


async def test_a_transient_error_is_retried(llm: _LLM, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "graph_retry_attempts", 2)
    flaky = _Flaky("flaky", failures=1, error=httpx.ConnectError("dropped"))

    result = await build_graph([flaky]).ainvoke(_state("", PageFacts(title="Lamp", price=1.0)))

    assert flaky.calls == 2
    assert "[flaky]\nok" in result["skill_results"]


async def test_after_the_last_attempt_the_skill_is_unavailable(llm: _LLM, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "graph_retry_attempts", 2)
    flaky = _Flaky("flaky", failures=5, error=ConnectionError("down"))

    result = await build_graph([flaky]).ainvoke(_state("", PageFacts(title="Lamp", price=1.0)))

    assert flaky.calls == 2
    assert "[flaky]\nSkill unavailable." in result["skill_results"]


async def test_other_errors_are_not_retried(llm: _LLM) -> None:
    flaky = _Flaky("flaky", failures=5, error=ValueError("bug"))

    result = await build_graph([flaky]).ainvoke(_state("", PageFacts(title="Lamp", price=1.0)))

    assert flaky.calls == 1
    assert "[flaky]\nSkill unavailable." in result["skill_results"]


def test_what_counts_as_transient() -> None:
    request = httpx.Request("GET", "https://x.example")
    assert transient(httpx.ReadTimeout("slow", request=request))
    assert transient(httpx.HTTPStatusError("x", request=request, response=httpx.Response(503, request=request)))
    assert not transient(httpx.HTTPStatusError("x", request=request, response=httpx.Response(404, request=request)))
    assert not transient(TimeoutError())
    assert not transient(ValueError())


# ── Page facts ──────────────────────────────────────────────────────────────────────────────────────────────

SERVICE_PAGE = """<html><head><script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Plumber", "name": "Loodgieter Snel B.V.", "telephone": "020-1234567",
 "email": "info@loodgietersnel.nl", "foundingDate": "1998",
 "areaServed": ["Amsterdam", {"@type": "City", "name": "Utrecht"}],
 "address": {"@type": "PostalAddress", "streetAddress": "Damrak 1", "postalCode": "1012 AB",
             "addressLocality": "Amsterdam"}}
</script></head><body>
<header><div class="account-menu">Hallo Jane, jane.doe@example.org, 06-12345678</div>
<a href="tel:0800-1234">Bel gratis</a></header>
<main><h1>Loodgieter Amsterdam, 24/7 bereikbaar</h1>
<p>Binnen 30 minuten ter plaatse. Geen voorrijkosten. Erkend installateur, sinds 1998. CA Lic. #1234567</p>
<div class="delivery-address">Jane Doe, Kerkstraat 5, 3511 AB Utrecht</div>
<div class="checkout-summary"><address>Jane Doe, Kerkstraat 5</address></div>
<form><input name="phone" value="0612345678"><textarea>Mijn adres: Kerkstraat 5</textarea></form>
<a href="/loodgieter-utrecht">Loodgieter Utrecht</a>
<a href="https://www.loodgietersnel.nl/loodgieter-haarlem">Haarlem</a>
<a href="https://elders.example/x">Elders</a>
<a href="mailto:planning@loodgietersnel.nl?subject=Afspraak">Mail</a></main>
<footer><div class="footer-address"><address>Damrak 1, 1012 AB Amsterdam</address></div>
Tel 020 123 4567 · Mail: snelloodgieter@gmail.com · KvK-nummer: 1234 5678 · IBAN NL91 ABNA 0417 1643 00 ·
Wij accepteren iDEAL, PayPal en Visa. <img src="k.png" alt="Klarna"> © 2026 Loodgieter Snel B.V.</footer>
</body></html>"""


def test_business_facts_come_from_what_the_business_publishes() -> None:
    facts, _ = parse_page(SERVICE_PAGE, "https://www.loodgietersnel.nl/")

    assert [(p.number, p.kind) for p in facts.phones] == [("+318001234", "toll_free"), ("+31201234567", "local")]
    assert [(e.address, e.kind) for e in facts.emails] == [
        ("planning@loodgietersnel.nl", "own_domain"),
        ("info@loodgietersnel.nl", "own_domain"),
        ("snelloodgieter@gmail.com", "webmail"),
    ]
    assert facts.addresses == ["Damrak 1, 1012 AB, Amsterdam", "Damrak 1, 1012 AB Amsterdam"]
    assert facts.payment_methods == ["paypal", "visa", "ideal", "klarna"]
    assert facts.kvk_numbers == ["12345678"]
    assert [(lic.number, lic.state) for lic in facts.licences] == [("1234567", "CA")]
    assert (facts.business_name, facts.legal_form) == ("Loodgieter Snel B.V.", "BV")
    assert facts.founded_claims == ["sinds 1998", "foundingDate 1998"]
    assert facts.cities_served == ["Amsterdam", "Utrecht"]
    assert any("Geen voorrijkosten" in t for t in facts.pricing_terms)
    assert any("24/7" in p for p in facts.promises) and any("30 minuten" in p for p in facts.promises)
    assert any("Erkend installateur" in c for c in facts.certifications)
    assert [link.url for link in facts.internal_links] == [
        "https://www.loodgietersnel.nl/loodgieter-utrecht",
        "https://www.loodgietersnel.nl/loodgieter-haarlem",
    ]
    assert facts.organization_ld[0]["@type"] == "Plumber"


def test_nothing_about_the_visitor_is_extracted() -> None:
    facts, _ = parse_page(SERVICE_PAGE, "https://www.loodgietersnel.nl/")

    dumped = facts.model_dump_json()
    for personal in ("jane", "Jane", "Kerkstraat", "0612345678", "+31612345678", "3511"):
        assert personal not in dumped


def test_a_product_page_gets_its_public_ids() -> None:
    page = (
        '<html><body><h1>Lamp</h1><script type="application/ld+json">'
        '{"@type": "Product", "name": "Lamp", "gtin13": "8712345678901", "mpn": "L-1"}</script></body></html>'
    )

    facts, _ = parse_page(page, "https://www.amazon.nl/dp/B0TEST1234?th=1")

    assert facts.product_ids == {"gtin13": "8712345678901", "mpn": "L-1", "asin": "B0TEST1234"}


@pytest.mark.parametrize(
    ("url", "platform"),
    [
        ("https://www.marktplaats.nl/v/x/m123456789-y", "marktplaats"),
        ("https://www.ebay.co.uk/itm/1", "ebay"),
        ("https://www.vinted.fr/items/1-a", "vinted"),
        ("https://www.2ememain.be/v/x/m123456789-y", "2dehands"),
        ("https://www.werkspot.nl/profiel/x", "werkspot"),
        ("https://www.my-hammer.de/auftragnehmer/x", "myhammer"),
        ("https://www.bol.com/nl/nl/p/x/1/", "bol.com"),
        ("https://notebay.example/", "generic"),
    ],
)
def test_platforms_are_recognised_by_host(url: str, platform: str) -> None:
    facts, _ = parse_page("<html><body><h1>x</h1></body></html>", url)

    assert facts.platform == platform


def test_a_private_sellers_name_is_never_kept() -> None:
    page = (
        '<html><body><h1>Fiets</h1><script type="application/ld+json">'
        '{"@type": "Product", "name": "Fiets", "offers": {"price": "150", "seller": {"name": "Jan de Vries"}}}'
        "</script></body></html>"
    )

    facts, _ = parse_page(page, AD_URL)

    assert facts.seller == ""
    assert "Jan de Vries" not in facts.model_dump_json()  # nor in the offer's structured data


@pytest.mark.usefixtures("expansion")
async def test_an_ebay_ad_by_a_private_seller_drops_their_name(llm: _LLM) -> None:
    url = "https://www.ebay.com/itm/123456789012"
    offer = {"price": "40", "seller": {"@type": "Person", "name": "jdevries82"}}
    facts = PageFacts(
        platform="ebay", url=url, title="Drill", price=40.0, seller="jdevries82", json_ld=[{"offers": offer}]
    )

    result = await build_graph([_Skill("a")]).ainvoke(_state(url, facts))

    assert result["page_type"] == "p2p_listing"
    assert "jdevries82" not in result["facts"].model_dump_json()


@pytest.mark.parametrize(
    ("expansion_on", "url", "facts", "page_type"),
    [
        (False, "https://lamp.example/p/1", PageFacts(title="Lamp", price=20.0), "storefront"),
        (True, "https://lamp.example/p/1", PageFacts(title="Lamp", price=20.0), "storefront"),
        (True, AD_URL, PageFacts(platform="marktplaats", url=AD_URL, title="Fiets", price=150.0), "p2p_listing"),
    ],
)
@pytest.mark.usefixtures("llm")
async def test_the_stream_route_reports_every_skill_step(
    monkeypatch: pytest.MonkeyPatch, expansion_on: bool, url: str, facts: PageFacts, page_type: str
) -> None:
    from unfret.routes import analyze  # noqa: PLC0415 - keep the app out of import time for the other tests

    monkeypatch.setattr(settings, "expansion_classes", expansion_on)
    graph = build_graph([_Skill("a"), _Skill("b")])
    final: dict[str, Any] = {}
    events: list[str] = []

    async for chunk in graph.astream(_state(url, facts), stream_mode="updates", subgraphs=True):
        _, update = chunk
        assert isinstance(update, dict)
        events += analyze._update_events(update, final)

    steps = [e for e in events if e.startswith("event: step")]
    assert any(f'"page_type": "{page_type}"' in e for e in steps)
    assert sum('"skill": "a"' in e for e in steps) == 1  # once, from the subgraph (the class node carries the score)
    assert sum('"skill": "b"' in e for e in steps) == 1
    assert "score" in final and "verdict" in final
