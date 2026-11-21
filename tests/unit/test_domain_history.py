"""The deep check's look at a shop's earlier copies in the Internet Archive: recorded copies, no network."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from unfret import card, fact_cache, public_result, shopper
from unfret.config import settings
from unfret.facts import PageFacts
from unfret.graph import SKILLS, triage
from unfret.graph.skills import domain_age, domain_history
from unfret.graph.skills.domain_age import DomainAgeSkill
from unfret.graph.skills.domain_history import DomainHistorySkill
from unfret.graph.state import Finding
from unfret.x402.verdict import skills_for

FIXTURES = Path(__file__).parents[1] / "fixtures" / "wayback"
TODAY = date(2026, 11, 20)

# The page now: an English-language sneaker shop on the old bakery's address.
SNEAKERS = (
    "Air Runner Pro sneakers. Limited stock! Only 3 left. Was $189.99, now $39.99. Add to cart. "
    "These sneakers are perfect for running and for the gym, with a breathable mesh upper and a cushioned sole. "
    "Free shipping worldwide on all sneakers. Our customers love these sneakers and we are sure that you will "
    "love them too. Order your sneakers now and get a second pair for half price. All our sneakers come with "
    "a money back guarantee, and you can return them for any reason. Shop sneakers, running shoes, trainers "
    "and more from the best brands for less. Sneakers running sneakers trainers shoes gym mesh sole."
)
# The page now: the outdoor shop's product page, the same shop as its earlier copy.
BOOTS = (
    "Ridgeline waterproof hiking boots. $149.00. Add to cart. Waterproof leather, grippy soles and a comfortable "
    "fit for long days on the trails. Tested by our team of hikers on real trails. Find the right size with our "
    "boots fit guide. Free shipping on orders over $75, free returns within 30 days. Every pair of boots ships "
    "within two working days from our warehouse. Pair them with merino hiking socks. Trailstone Outfitters boots "
    "hiking trails waterproof outdoor gear jackets shoes."
)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def archive(copies: dict[str, str], cdx: str = "cdx.json") -> Any:
    """A stand-in for fetch_public: the CDX answer, then each copy by its timestamp."""
    asked: list[str] = []

    async def fetch(url: str, **kwargs: Any) -> bytes:
        asked.append(url)
        assert kwargs["max_bytes"] <= 3 * 1024 * 1024 and kwargs["total_timeout"] <= 15
        if "/cdx/search/cdx" in url:
            return fixture(cdx)
        timestamp = url.split("/web/", 1)[1].split("id_/", 1)[0]
        if timestamp not in copies:
            raise httpx.ReadTimeout("archive slow")
        return fixture(copies[timestamp])

    fetch.asked = asked  # type: ignore[attr-defined]
    return fetch


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fact_cache, "_store", fact_cache.MemoryStore())
    monkeypatch.setattr(settings, "network_lookups", True)
    monkeypatch.setattr(settings, "domain_history", True)
    monkeypatch.setattr(settings, "wayback_url", "https://web.archive.org")
    monkeypatch.setattr(settings, "wayback_per_minute", 8)
    monkeypatch.setattr(domain_history, "pace", domain_history.new_pace())
    real = domain_history.history

    async def history(host: str, today: date | None = None) -> dict[str, Any]:
        return await real(host, today or TODAY)

    monkeypatch.setattr(domain_history, "history", history)


def state(url: str, text: str, facts: PageFacts | None = None, years: float = 12.0) -> Any:
    return {
        "page_content": "",
        "url": url,
        "flags": [],
        "facts": facts or PageFacts(),
        "cleaned_content": text,
        "page_type": "storefront",
        "features": {"domain_age.known": 1.0, "domain_age.age_years": years},
        "findings": [],
    }


# ── Reading a copy ────────────────────────────────────────────────────────────


def test_what_a_copy_shows() -> None:
    kinds = {p.name: domain_history.summarise(fixture(p.name).decode())["kind"] for p in FIXTURES.glob("*.html")}
    assert kinds == {
        "bakery.html": "site",
        "parked.html": "parked",
        "placeholder.html": "placeholder",
        "thin.html": "thin",
        "shop_then.html": "shop",
    }
    bakery = domain_history.summarise(fixture("bakery.html").decode())
    assert bakery["lang"] == "nl" and bakery["title"].startswith("Bakkerij De Oude Bakker")
    assert "brood" in bakery["words"] and "bakkerij" in bakery["tokens"]
    assert domain_history.language(SNEAKERS) == "en" and domain_history.language("Brood") == ""


def test_the_copies_closest_to_one_three_and_twelve_months_ago() -> None:
    rows = json.loads(fixture("cdx.json"))[1:]
    picked = domain_history.pick(rows, TODAY)
    assert [r[0][:8] for r in picked] == ["20261018", "20260815", "20251203"]
    # Nothing near a target: left out, never stretched to a copy years off.
    assert domain_history.pick([["20200101000000", "https://x.example/"]], TODAY) == []
    assert domain_history.month_of("20260302120000") == "March 2026"


def test_a_shop_mentioning_coming_soon_is_no_placeholder() -> None:
    # A real shop page of a JavaScript-built shop: little text, a product "coming soon", a domain seller's link.
    page = (
        "<html><head><title>Laptops, TVs and phones | Bigstore</title></head><body><h1>Bigstore</h1>"
        "<p>PS6 Pro coming soon. Pre-order now and be the first to play. Free delivery on orders over 50.</p>"
        "<p>Laptops TVs phones gaming appliances smart home deals of the week, gift cards and more.</p>"
        "<a href='https://dan.com/x'>sell a domain</a></body></html>"
    )
    assert domain_history.summarise(page)["kind"] not in {"placeholder", "parked"}


def test_comparison_rules() -> None:
    bakery = domain_history.summarise(fixture("bakery.html").decode())
    shop = domain_history.summarise(fixture("shop_then.html").decode())
    thin = domain_history.summarise(fixture("thin.html").decode())
    now: dict[str, Any] = {"lang_now": "en", "words_now": domain_history.top_words(SNEAKERS)}
    assert domain_history.compare(bakery, **now, names=["sneakerworld"]) == "different"
    # The address's own name in the old site doesn't make it the same site when everything else differs ...
    assert domain_history.compare(bakery, **now, names=[], label="oudebakker") == "different"
    # ... but the shop's own name does, whatever else differs (a brand's global country chooser in another language).
    assert domain_history.compare(bakery, **now, names=["oudebakker"]) == "same"
    assert domain_history.compare(thin, **now, names=[]) == "unclear"  # a page its scripts build says nothing
    boots: dict[str, Any] = {"lang_now": "en", "words_now": domain_history.top_words(BOOTS)}
    assert domain_history.compare(shop, **boots, names=[], label="trailstone") == "same"
    # Another shop in the same language, without the name: maybe a new owner, maybe a new name; not enough.
    assert domain_history.compare(shop, **now, names=["sneakerworld"]) == "unclear"
    # A language change alone (a shop's English home page for the archive's crawler) is never enough.
    assert domain_history.compare(shop, lang_now="nl", words_now=boots["words_now"], names=[]) == "unclear"


# ── The skill ─────────────────────────────────────────────────────────────────


async def test_an_old_address_that_was_a_bakery_and_a_parked_page(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = archive(
        {"20261018093000": "parked.html", "20260815101500": "parked.html", "20251203090101": "bakery.html"}
    )
    monkeypatch.setattr(domain_history, "fetch_public", fetch)
    out = await DomainHistorySkill().run(state("https://www.oudebakker.example/p/air-runner", SNEAKERS))

    f = out["features"]
    assert f["domain_history.changed"] == 1.0 and f["domain_history.parked"] == 1.0
    assert f["domain_history.snapshots"] == 3.0 and f["domain_history.months_since_change"] == pytest.approx(1.1)
    repurposed = next(x for x in out["findings"] if x.type == "domain_repurposed")
    assert repurposed.evidence.startswith(
        "oudebakker.example is 12 years old, but the Internet Archive's copy from October 2026 shows a parked page"
    )
    assert "https://web.archive.org/web/20251203090101/https://www.oudebakker.example/" in repurposed.evidence
    assert len([x for x in out["findings"] if x.type == "archive_copy"]) == 3
    # Raw copies only (id_), from the archive's index for the address's home page; never cookies or the shop itself.
    assert all(u.startswith("https://web.archive.org/") for u in fetch.asked)
    assert "url=oudebakker.example/" in fetch.asked[0]
    assert all("id_/" in u for u in fetch.asked[1:])


async def test_the_same_shop_says_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    copies = dict.fromkeys(("20261018093000", "20260815101500", "20251203090101"), "shop_then.html")
    monkeypatch.setattr(domain_history, "fetch_public", archive(copies))
    facts = PageFacts(meta={"og:site_name": "Trailstone Outfitters"})
    out = await DomainHistorySkill().run(state("https://trailstone.example/products/ridgeline", BOOTS, facts))
    assert out["features"].get("domain_history.changed", 0.0) == 0.0
    assert not [x for x in out["findings"] if x.type == "domain_repurposed"]


async def test_no_copies_is_said_neutrally(monkeypatch: pytest.MonkeyPatch) -> None:
    async def empty(url: str, **_: Any) -> bytes:
        return b"[]"

    monkeypatch.setattr(domain_history, "fetch_public", empty)
    out = await DomainHistorySkill().run(state("https://new.example/p", SNEAKERS))
    assert out["features"] == {"domain_history.checked": 1.0, "domain_history.snapshots": 0.0}
    [finding] = out["findings"]
    assert finding.type == "no_archive_copies" and "no copies" in finding.evidence
    verdict = shopper.build(70, features=out["features"], findings=out["findings"], facts=PageFacts(),
                            page_type="storefront")  # fmt: skip
    assert "domain_repurposed" not in {r.type for r in verdict.reasons}


async def test_an_unreachable_archive_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(domain_history, "fetch_public", archive({}))
    with pytest.raises(domain_history.ArchiveUnreachableError):  # a ConnectionError: the graph retries it once
        await DomainHistorySkill().run(state("https://www.oudebakker.example/p", SNEAKERS))
    monkeypatch.setattr(domain_history, "fetch_public", archive({"20251203090101": "bakery.html"}))
    out = await DomainHistorySkill().run(state("https://www.oudebakker.example/p", SNEAKERS))
    assert out["features"]["domain_history.changed"] == 1.0


async def test_a_pace_of_its_own_and_a_pause_after_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    # The archive blocks clients that ask too often: Unfret asks a few times a minute at most, and stops asking for
    # a while once it refused a connection, instead of retrying into a longer block.
    monkeypatch.setattr(settings, "wayback_per_minute", 4)
    monkeypatch.setattr(domain_history, "fetch_public", archive({"20251203090101": "bakery.html"}))
    await DomainHistorySkill().run(state("https://www.oudebakker.example/p", SNEAKERS))  # 1 index + 3 copies
    with pytest.raises(domain_history.ArchiveBusyError):
        await DomainHistorySkill().run(state("https://other.example/p", SNEAKERS))

    monkeypatch.setattr(domain_history, "pace", domain_history.new_pace())
    calls: list[str] = []

    async def refused(url: str, **_: Any) -> bytes:
        calls.append(url)
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(domain_history, "fetch_public", refused)
    with pytest.raises(httpx.ConnectError):
        await DomainHistorySkill().run(state("https://a.example/p", SNEAKERS))
    with pytest.raises(domain_history.ArchiveBusyError):  # not a ConnectionError: the graph doesn't retry it
        await DomainHistorySkill().run(state("https://b.example/p", SNEAKERS))
    assert len(calls) == 1


async def test_off_without_network_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "network_lookups", False)
    out = await DomainHistorySkill().run(state("https://www.oudebakker.example/p", SNEAKERS))
    assert out["features"] == {"domain_history.checked": 0.0}


# ── What the shopper sees ─────────────────────────────────────────────────────


async def _changed(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, float], list[Finding]]:
    monkeypatch.setattr(domain_history, "fetch_public", archive({"20251203090101": "bakery.html"}))
    out = await DomainHistorySkill().run(state("https://www.oudebakker.example/p", SNEAKERS))
    return {"domain_age.known": 1.0, "domain_age.age_years": 12.4, **out["features"]}, out["findings"]


async def test_the_reason_with_its_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    features, findings = await _changed(monkeypatch)
    verdict = shopper.build(60, features=features, findings=findings, facts=PageFacts(), page_type="storefront")
    reason = next(r for r in verdict.reasons if r.type == "domain_repurposed")
    assert reason.title == "Older address, new shop"
    assert reason.detail == (
        "This web address is 12 years old, but as recently as December 2025 it showed a different site (not a shop, "
        "in Dutch, “Bakkerij De Oude Bakker - ambachtelijk brood uit Zwolle”). So its age says little about the "
        "shop on it now."
    )
    assert reason.links == ["https://web.archive.org/web/20251203090101/https://www.oudebakker.example/"]
    assert "scam" not in (reason.title + reason.detail).lower()
    # The address's age no longer counts in the shop's favour.
    assert "established_domain" not in {p.type for p in verdict.positives}

    carded = card.complete(verdict.model_dump(), state("https://www.oudebakker.example/p", SNEAKERS) | {
        "features": features, "findings": findings}, available=SKILLS, applicable=SKILLS)  # fmt: skip
    shown = next(r for r in carded["reasons"] if r["type"] == "domain_repurposed")
    assert shown["source"] == "Earlier copies of the site (Internet Archive, web.archive.org)"
    who = next(i for i in carded["checklist"] if i["key"] == "who")
    assert who["status"] == "concern"


async def test_never_looks_fine_with_it(monkeypatch: pytest.MonkeyPatch) -> None:
    features, findings = await _changed(monkeypatch)
    verdict = shopper.build(95, features=features, findings=findings, facts=PageFacts(), page_type="storefront")
    assert verdict.band == "concerns"


def test_a_shared_result_keeps_only_archive_links() -> None:
    reason = {"type": "domain_repurposed", "title": "Older address, new shop", "detail": "d", "severity": "medium",
              "links": ["https://web.archive.org/web/2025/https://a.example/", "https://evil.example/"]}  # fmt: skip
    shown = public_result.public_result({"score": 60, "verdict": {"band": "concerns", "reasons": [reason]}})
    assert shown["verdict"]["reasons"][0]["links"] == ["https://web.archive.org/web/2025/https://a.example/"]


# ── When the deep check looks ─────────────────────────────────────────────────


def _triage_state(years: float, features: dict[str, float] | None = None, **extra: Any) -> Any:
    return {
        "facts": PageFacts(),
        "features": {"domain_age.age_years": years, **(features or {})},
        "findings": [],
        "page_type": "storefront",
        "url": "https://old.example/p",
        "cleaned_content": "",
        **extra,
    }


DOUBTFUL = {"content.generic_product": 1.0, "listing.generated_brand": 0.8, "content.scam_copy": 0.6}


def test_only_an_old_address_on_a_doubtful_page() -> None:
    available = {"domain_history"}
    assert triage.history_plan(_triage_state(12, DOUBTFUL), available).run == ("domain_history",)
    assert triage.history_plan(_triage_state(1, DOUBTFUL), available).run == ()  # young: its age vouches for nothing
    assert triage.history_plan(_triage_state(12, {"content.brand_recognized": 1.0}), available).run == ()
    assert triage.history_plan(_triage_state(12, DOUBTFUL, expensive_runs=["domain_history"]), available).run == ()
    assert triage.history_plan(_triage_state(12, DOUBTFUL, page_type="aggregate_listing"), available).run == ()
    assert triage.history_plan(_triage_state(12, DOUBTFUL), set()).run == ()


def test_doubtful_follows_the_verdict_rules_with_a_scorer(monkeypatch: pytest.MonkeyPatch) -> None:
    class Model:
        needs_reason = True

    risks = {"risk": 0.1}

    def score(features: Any, findings: Any, model: Any) -> Any:
        return type("A", (), {"risk_score": risks["risk"], "confidence": 0.9})()

    monkeypatch.setattr(triage.scoring, "load_model", lambda path=None: Model())
    monkeypatch.setattr(triage.scoring, "score", score)
    assert not triage.looks_doubtful(_triage_state(12, DOUBTFUL))  # the scorer says 90: "Looks fine"
    risks["risk"] = 0.6
    assert triage.looks_doubtful(_triage_state(12, DOUBTFUL))  # 40: "Think twice"


def test_off_switch_and_quick_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "domain_history" not in {s.name for s in skills_for("quick", SKILLS)}
    monkeypatch.setattr(settings, "domain_history", False)
    assert triage.history_plan(_triage_state(12, DOUBTFUL), {"domain_history"}).run == ()


async def test_planned_next_to_the_photo_search_and_outside_its_budget() -> None:
    state = _triage_state(12, DOUBTFUL, facts=PageFacts(image_urls=["https://old.example/1.jpg"]))
    planned = await triage.plan(state, {"reverse_image", "domain_history"})
    assert planned.run == ("reverse_image", "domain_history")
    # The round after: the history doesn't use up the photo budget, and isn't planned twice.
    after = state | {"triage_round": 1, "expensive_runs": ["domain_history"]}
    assert (await triage.plan(after, {"reverse_image", "domain_history"})).run == ()


# ── The archive's first copy as the address's age, where the registry gives none ─────────────────────────────


def _registry(rdap: dict[str, Any] | None, certificates: list[dict[str, str]]) -> Any:
    """A stand-in for domain_age's fetch_public: the RDAP answer (``None``: not found) and crt.sh's."""
    request = httpx.Request("GET", "https://rdap.test/")

    async def fetch(url: str, **_: Any) -> bytes:
        if "crt.sh" in url:
            return json.dumps(certificates).encode()
        if rdap is None:
            raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(404, request=request))
        return json.dumps(rdap).encode()

    return fetch


# A .nl-style answer: the registry knows the domain but publishes no registration date.
NO_DATE = {"events": [{"eventAction": "last changed", "eventDate": "2024-02-01"}], "entities": []}


def _first_copy() -> Any:
    asked: list[str] = []

    async def fetch(url: str, **kwargs: Any) -> bytes:
        asked.append(url)
        assert kwargs["max_bytes"] <= 16 * 1024 and kwargs["total_timeout"] <= 15
        return fixture("cdx_first.json")

    fetch.asked = asked  # type: ignore[attr-defined]
    return fetch


def _age_state(url: str, page_type: str = "storefront") -> Any:
    return state(url, BOOTS) | {"page_type": page_type, "features": {}}


async def test_first_capture_is_one_paced_index_request(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = _first_copy()
    monkeypatch.setattr(domain_history, "fetch_public", fetch)
    assert await domain_history.first_capture("www.trailstone.example") == "2009-04-12"
    [url] = fetch.asked
    assert url.startswith("https://web.archive.org/cdx/search/cdx?url=www.trailstone.example/")
    assert "limit=1" in url and "fl=timestamp" in url and "&from=" not in url  # the earliest copy, only its date
    assert len(domain_history.pace.sent) == 1  # within the same pace as the history look

    async def empty(url: str, **_: Any) -> bytes:
        return b"[]"

    monkeypatch.setattr(domain_history, "fetch_public", empty)
    assert await domain_history.first_capture("new.example") is None


async def test_an_age_from_the_archive_when_the_registry_gives_no_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(domain_age, "fetch_public", _registry(NO_DATE, [{"not_before": "2025-06-01T00:00:00"}]))
    fetch = _first_copy()
    monkeypatch.setattr(domain_history, "fetch_public", fetch)
    out = await DomainAgeSkill().run(_age_state("https://www.trailstone.example/p"))
    f = out["features"]
    assert f["domain_age.age_from_archive"] == 1.0 and f["domain_age.archive_first_year"] == 2009.0
    assert f["domain_age.archive_age_years"] > 17
    # The registry's own age is untouched: the archive's is a minimum, kept apart, never the registration date.
    assert f["domain_age.age_years"] < 2
    first = next(x for x in out["findings"] if x.type == "archive_first_capture")
    assert first.evidence.startswith("Online since at least 2009-04-12, per the Internet Archive")
    assert "not the registration date" in first.evidence
    assert not [x for x in out["findings"] if x.type == "registered"]
    # Cached per address like the history look: the next check doesn't ask again.
    await DomainAgeSkill().run(_age_state("https://www.trailstone.example/q"))
    assert len(fetch.asked) == 1


async def test_nothing_from_the_registry_or_the_certificates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(domain_age, "fetch_public", _registry(None, []))
    monkeypatch.setattr(domain_history, "fetch_public", _first_copy())
    out = await DomainAgeSkill().run(_age_state("https://trailstone.example/p"))
    f = out["features"]
    assert f["domain_age.known"] == 0.0  # the registry is still silent; the archive only says "at least"
    assert f["domain_age.age_from_archive"] == 1.0 and f["domain_age.archive_first_year"] == 2009.0


async def test_no_archive_question_with_a_registration_date_or_off_a_shop(monkeypatch: pytest.MonkeyPatch) -> None:
    registered = {"events": [{"eventAction": "registration", "eventDate": "2015-03-01"}]}
    monkeypatch.setattr(domain_age, "fetch_public", _registry(registered, []))
    fetch = _first_copy()
    monkeypatch.setattr(domain_history, "fetch_public", fetch)
    out = await DomainAgeSkill().run(_age_state("https://trailstone.example/p"))
    assert "domain_age.archive_age_years" not in out["features"]
    monkeypatch.setattr(domain_age, "fetch_public", _registry(NO_DATE, []))
    await DomainAgeSkill().run(_age_state("https://plumber.example/", page_type="service_site"))
    monkeypatch.setattr(settings, "domain_history", False)
    await DomainAgeSkill().run(_age_state("https://other.example/p"))
    assert fetch.asked == []


async def test_paced_out_or_refused_it_is_skipped_and_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(domain_age, "fetch_public", _registry(NO_DATE, []))
    monkeypatch.setattr(domain_history, "fetch_public", _first_copy())
    monkeypatch.setattr(domain_history.pace, "paused_until", float("inf"))  # the archive refused us lately
    out = await DomainAgeSkill().run(_age_state("https://trailstone.example/p"))
    assert out["features"]["domain_age.known"] == 1.0 and "domain_age.archive_age_years" not in out["features"]

    async def refused(url: str, **_: Any) -> bytes:
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(domain_history, "pace", domain_history.new_pace())
    monkeypatch.setattr(domain_history, "fetch_public", refused)
    out = await DomainAgeSkill().run(_age_state("https://trailstone.example/p"))
    assert "domain_age.archive_age_years" not in out["features"]
    assert domain_history.pace.paused_until > 0  # the refusal pauses the archive for the history look too

    monkeypatch.setattr(domain_history, "pace", domain_history.new_pace())
    monkeypatch.setattr(domain_history, "fetch_public", _first_copy())
    out = await DomainAgeSkill().run(_age_state("https://trailstone.example/p"))
    assert out["features"]["domain_age.archive_first_year"] == 2009.0  # neither was remembered


ARCHIVE_AGE = {
    "domain_age.known": 1.0,
    "domain_age.age_years": 0.0,
    "domain_age.archive_age_years": 17.6,
    "domain_age.archive_first_year": 2009.0,
    "domain_age.age_from_archive": 1.0,
}


def test_an_archive_age_counts_only_once_the_history_look_agrees() -> None:
    def positives(features: dict[str, float]) -> set[str]:
        verdict = shopper.build(80, features=features, findings=[], facts=PageFacts(), page_type="storefront")
        return {p.type for p in verdict.positives}

    assert shopper.address_years(ARCHIVE_AGE) == (17.6, True)
    # Before (or without) a look at its earlier copies: no "Established shop" from the archive's age alone.
    assert not positives(ARCHIVE_AGE) & {"established_domain", "established_archive"}
    assert not positives(ARCHIVE_AGE | {"domain_history.checked": 1.0, "domain_history.snapshots": 0.0}) & {
        "established_archive"
    }
    # Its recent copies show the same shop: established, by the archive, said as a minimum.
    agreed = ARCHIVE_AGE | {"domain_history.checked": 1.0, "domain_history.snapshots": 3.0}
    assert "established_archive" in positives(agreed) and "established_domain" not in positives(agreed)
    verdict = shopper.build(80, features=agreed, findings=[], facts=PageFacts(), page_type="storefront")
    established = next(p for p in verdict.positives if p.type == "established_archive")
    assert established.title == "Established shop"
    assert established.detail.startswith("This web shop's address has been online since at least 2009, per the")
    carded = card.complete(verdict.model_dump(), state("https://trailstone.example/p", BOOTS) | {
        "features": agreed, "findings": []}, available=SKILLS, applicable=SKILLS)  # fmt: skip
    shown = next(p for p in carded["positives"] if p["type"] == "established_archive")
    assert shown["source"] == "Earlier copies of the site (Internet Archive, web.archive.org)"
    # A registration date always wins over the archive's minimum.
    assert shopper.address_years(ARCHIVE_AGE | {"domain_age.age_years": 20.0}) == (20.0, False)


async def test_an_archive_age_taken_back_by_the_history(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(domain_history, "fetch_public", archive({"20251203090101": "bakery.html"}))
    out = await DomainHistorySkill().run(
        state("https://www.oudebakker.example/p", SNEAKERS) | {"features": ARCHIVE_AGE}
    )
    repurposed = next(x for x in out["findings"] if x.type == "domain_repurposed")
    assert repurposed.evidence.startswith(
        "oudebakker.example has been online since at least 2009 (per the Internet Archive), but the Internet "
        "Archive's copy from December 2025 shows a different site"
    )
    features = ARCHIVE_AGE | out["features"]
    verdict = shopper.build(80, features=features, findings=out["findings"], facts=PageFacts(),
                            page_type="storefront")  # fmt: skip
    reason = next(r for r in verdict.reasons if r.type == "domain_repurposed")
    assert reason.detail.startswith("This web address is at least 17 years old, but as recently as December 2025")
    assert not {p.type for p in verdict.positives} & {"established_domain", "established_archive"}


def test_an_archive_age_triggers_the_history_look() -> None:
    available = {"domain_history"}
    planned = triage.history_plan(_triage_state(0, DOUBTFUL | ARCHIVE_AGE), available)
    assert planned.run == ("domain_history",) and "Internet Archive" in planned.reason
    young = ARCHIVE_AGE | {"domain_age.archive_age_years": 1.5}
    assert triage.history_plan(_triage_state(0, DOUBTFUL | young), available).run == ()
