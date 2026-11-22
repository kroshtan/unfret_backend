"""Common Crawl first, the Internet Archive after, and Unfret's own first sighting: recorded answers, no network."""

import gzip
import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from unfret import card, fact_cache, observations, public_result, shop_store, shopper
from unfret.config import settings
from unfret.facts import PageFacts
from unfret.graph import SKILLS
from unfret.graph.skills import common_crawl, domain_age, domain_history
from unfret.graph.skills.domain_age import DomainAgeSkill
from unfret.graph.skills.domain_history import DomainHistorySkill
from unfret.pace import Pace

FIXTURES = Path(__file__).parents[1] / "fixtures"
TODAY = date(2026, 11, 20)
SNEAKERS = (
    "Air Runner Pro sneakers. Limited stock! Only 3 left. Was $189.99, now $39.99. Add to cart. "
    "These sneakers are perfect for running and for the gym, with a breathable mesh upper and a cushioned sole. "
    "Free shipping worldwide on all sneakers. Our customers love these sneakers and we are sure that you will "
    "love them too. Order your sneakers now and get a second pair for half price. All our sneakers come with "
    "a money back guarantee, and you can return them for any reason. Shop sneakers, running shoes, trainers "
    "and more from the best brands for less. Sneakers running sneakers trainers shoes gym mesh sole."
)


def page(name: str) -> bytes:
    return (FIXTURES / "wayback" / name).read_bytes()


def crawl_fixture(name: str) -> Any:
    return json.loads((FIXTURES / "commoncrawl" / name).read_text())


def warc(html: bytes, *, record_type: str = "response", http_headers: str = "") -> bytes:
    """One gzipped WARC record, as a byte range of a Common Crawl WARC file is."""
    http = (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nX-Crawler-Content-Encoding: gzip\r\n"
        + http_headers.encode()
        + f"Content-Length: {len(html)}\r\n\r\n".encode()
        + html
    )
    head = (
        f"WARC/1.0\r\nWARC-Type: {record_type}\r\nWARC-Target-URI: https://www.oudebakker.example/\r\n"
        f"Content-Type: application/http; msgtype=response\r\nContent-Length: {len(http)}\r\n\r\n"
    ).encode()
    return gzip.compress(head + http + b"\r\n\r\n")


def _status(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://index.commoncrawl.org/x")
    return httpx.HTTPStatusError(str(code), request=request, response=httpx.Response(code, request=request))


def crawl_server(pages: dict[str, str], index: str = "index.json", fail: int | None = None) -> Any:
    """A stand-in for common_crawl's fetch_public: the crawl list, each crawl's index, and WARC records by range."""
    lines: dict[str, list[str]] = crawl_fixture(index)
    asked: list[tuple[str, dict[str, Any]]] = []

    async def fetch(url: str, **kwargs: Any) -> bytes:
        asked.append((url, kwargs))
        assert kwargs["total_timeout"] <= 15
        if fail is not None:
            raise _status(fail)
        if url == "https://index.commoncrawl.org/collinfo.json":
            return (FIXTURES / "commoncrawl" / "collinfo.json").read_bytes()
        if url.startswith("https://index.commoncrawl.org/"):
            crawl = url.split("/")[3].split("-index?", maxsplit=1)[0]
            if crawl not in lines:
                raise _status(404)  # "No Captures found"
            return "\n".join(lines[crawl]).encode()
        assert url.startswith("https://data.commoncrawl.org/crawl-data/")
        crawl = url.split("/")[4]
        if crawl not in pages:
            raise httpx.ReadTimeout("slow")
        return warc(page(pages[crawl]))

    fetch.asked = asked  # type: ignore[attr-defined]
    return fetch


def no_wayback(seen: list[str] | None = None) -> Any:
    async def fetch(url: str, **_: Any) -> bytes:
        if seen is None:
            raise AssertionError(f"asked the Internet Archive: {url}")
        seen.append(url)
        return b"[]"

    return fetch


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fact_cache, "_store", fact_cache.MemoryStore())
    monkeypatch.setattr(shop_store, "_store", shop_store.MemoryShopStore())
    monkeypatch.setattr(settings, "network_lookups", True)
    monkeypatch.setattr(settings, "domain_history", True)
    monkeypatch.setattr(settings, "wayback_per_minute", 8)
    real_history, real_first = domain_history.history, common_crawl.first_capture

    async def history(host: str, today: date | None = None) -> dict[str, Any]:
        return await real_history(host, today or TODAY)

    async def first_capture(host: str, today: date | None = None) -> str | None:
        return await real_first(host, today or TODAY)

    monkeypatch.setattr(domain_history, "history", history)
    monkeypatch.setattr(common_crawl, "first_capture", first_capture)


def state(url: str, text: str = SNEAKERS, years: float = 12.0, page_type: str = "storefront") -> Any:
    return {
        "page_content": "",
        "url": url,
        "flags": [],
        "facts": PageFacts(),
        "cleaned_content": text,
        "page_type": page_type,
        "features": {"domain_age.known": 1.0, "domain_age.age_years": years},
        "findings": [],
    }


# ── Reading Common Crawl ──────────────────────────────────────────────────────


def test_the_crawls_nearest_one_three_and_twelve_months_ago() -> None:
    crawls = common_crawl.parse_crawls(crawl_fixture("collinfo.json"))
    assert "broken" not in {c.id for c in crawls}  # no dates: never picked
    picked = common_crawl.pick(crawls, TODAY, domain_history.TARGETS_DAYS)
    assert [c.id for c in picked] == ["CC-MAIN-2026-43", "CC-MAIN-2026-34", "CC-MAIN-2025-47"]
    # Nothing near a target: left out, never stretched to a crawl years off.
    assert common_crawl.pick(crawls, date(2040, 1, 1), domain_history.TARGETS_DAYS) == []


def test_a_warc_record_gives_its_page() -> None:
    assert common_crawl.page_of(warc(page("bakery.html"))) == page("bakery.html")
    # A body still encoded (older crawls kept the header): decoded within the limit.
    encoded = warc(gzip.compress(page("bakery.html")), http_headers="Content-Encoding: gzip\r\n")
    assert common_crawl.page_of(encoded) == page("bakery.html")
    chunked = warc(b"5\r\nHallo\r\n0\r\n\r\n", http_headers="Transfer-Encoding: chunked\r\n")
    assert common_crawl.page_of(chunked) == b"Hallo"
    for broken in (
        warc(page("bakery.html"), record_type="request"),
        b"not gzip",
        warc(b"x", http_headers="Content-Encoding: br\r\n"),
    ):
        with pytest.raises(common_crawl.RecordError):
            common_crawl.page_of(broken)
    huge = gzip.compress(b"WARC/1.0\r\nWARC-Type: response\r\n\r\n" + b"a" * (4 * 1024 * 1024))
    with pytest.raises(common_crawl.RecordError):
        common_crawl.page_of(huge)


def test_only_html_records_within_the_limit() -> None:
    rows = [json.loads(r) for r in crawl_fixture("index.json")["CC-MAIN-2026-43"]]
    assert common_crawl.html_capture(rows) == rows[1]  # the first is a PDF
    assert common_crawl.html_capture([rows[1] | {"length": str(2 * 1024 * 1024)}]) is None
    assert common_crawl.html_capture([rows[1] | {"filename": "crawl-data/../../etc/passwd.warc.gz"}]) is None


# ── The look at earlier copies ────────────────────────────────────────────────


async def test_common_crawl_first_with_links_to_its_records(monkeypatch: pytest.MonkeyPatch) -> None:
    server = crawl_server(
        {"CC-MAIN-2026-43": "parked.html", "CC-MAIN-2026-34": "parked.html", "CC-MAIN-2025-47": "bakery.html"}
    )
    monkeypatch.setattr(common_crawl, "fetch_public", server)
    monkeypatch.setattr(domain_history, "fetch_public", no_wayback())  # never asked: Common Crawl had copies
    out = await DomainHistorySkill().run(state("https://www.oudebakker.example/p/air-runner"))

    f = out["features"]
    assert f["domain_history.changed"] == 1.0 and f["domain_history.parked"] == 1.0
    assert f["domain_history.snapshots"] == 3.0
    repurposed = next(x for x in out["findings"] if x.type == "domain_repurposed")
    assert repurposed.evidence.startswith(
        "oudebakker.example is 12 years old, but Common Crawl's copy from October 2026 shows a parked page"
    )
    link = (
        "https://index.commoncrawl.org/CC-MAIN-2025-47-index?url=https://oudebakker.example/&from=20251110090101"
        "&limit=1"
    )
    assert link in repurposed.evidence and len(repurposed.evidence) <= 500
    copies = [x.evidence for x in out["findings"] if x.type == "archive_copy"]
    assert copies[0].startswith("Common Crawl copy of oudebakker.example from October 2026: a parked page")

    # The crawl list once, an index request per picked crawl, a record by byte range per copy; nothing else.
    urls = [u for u, _ in server.asked]
    assert urls[0] == "https://index.commoncrawl.org/collinfo.json"
    assert sum(u.startswith("https://index.commoncrawl.org/CC-MAIN-") for u in urls) == 3
    assert all("url=oudebakker.example/" in u for u in urls[1:4])
    records = [(u, kw) for u, kw in server.asked if u.startswith("https://data.commoncrawl.org/")]
    assert len(records) == 3
    second = next(kw for u, kw in records if "CC-MAIN-2026-43" in u)
    assert second["headers"] == {"Range": "bytes=2000-10999"} and second["max_bytes"] == 9000

    # What the shopper sees: the reason, its links and its source name Common Crawl.
    features = {"domain_age.known": 1.0, "domain_age.age_years": 12.4, **f}
    verdict = shopper.build(60, features=features, findings=out["findings"], facts=PageFacts(), page_type="storefront")
    reason = next(r for r in verdict.reasons if r.type == "domain_repurposed")
    assert reason.detail.startswith(
        "This web address is 12 years old, but as recently as October 2026 it showed a parked"
    )
    assert reason.links and all(u.startswith("https://index.commoncrawl.org/CC-MAIN-") for u in reason.links)
    carded = card.complete(verdict.model_dump(), state("https://www.oudebakker.example/p") | {
        "features": features, "findings": out["findings"]}, available=SKILLS, applicable=SKILLS)  # fmt: skip
    shown = next(r for r in carded["reasons"] if r["type"] == "domain_repurposed")
    assert shown["source"] == "Earlier copies of the site (Common Crawl, commoncrawl.org)"
    shared = public_result.public_result({"score": 60, "verdict": {"band": "concerns", "reasons": [shown]}})
    assert shared["verdict"]["reasons"][0]["links"] == reason.links

    # Cached per address: the next check asks no one.
    before = len(server.asked)
    await DomainHistorySkill().run(state("https://www.oudebakker.example/q"))
    assert len(server.asked) == before


async def test_the_internet_archive_only_without_a_usable_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    # Common Crawl has the address only as pages too thin to read: the archive is asked, and its copies count.
    server = crawl_server(dict.fromkeys(("CC-MAIN-2026-43", "CC-MAIN-2026-34", "CC-MAIN-2025-47"), "thin.html"))
    monkeypatch.setattr(common_crawl, "fetch_public", server)
    wayback: list[str] = []

    async def archive(url: str, **_: Any) -> bytes:
        wayback.append(url)
        if "/cdx/search/cdx" in url:
            return page("cdx.json")
        return page("bakery.html")

    monkeypatch.setattr(domain_history, "fetch_public", archive)
    out = await DomainHistorySkill().run(state("https://www.oudebakker.example/p"))
    repurposed = next(x for x in out["findings"] if x.type == "domain_repurposed")
    assert (
        "the Internet Archive's copy from" in repurposed.evidence
        and "https://web.archive.org/web/" in repurposed.evidence
    )
    assert wayback and wayback[0].startswith("https://web.archive.org/cdx/")


async def test_nothing_anywhere_is_said_neutrally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common_crawl, "fetch_public", crawl_server({}, index="index_first.json"))
    asked: list[str] = []
    monkeypatch.setattr(domain_history, "fetch_public", no_wayback(asked))
    out = await DomainHistorySkill().run(state("https://new.example/p"))
    assert out["features"] == {"domain_history.checked": 1.0, "domain_history.snapshots": 0.0}
    [finding] = out["findings"]
    assert finding.evidence == (
        "Common Crawl and the Internet Archive have no copies of new.example from the past year to compare with."
    )
    assert asked  # Common Crawl had none: the archive was asked


async def test_with_the_archives_daily_cap_used_common_crawls_answer_stands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "wayback_daily_cap", 0)  # never ask the Internet Archive
    monkeypatch.setattr(common_crawl, "fetch_public", crawl_server({}, index="index_first.json"))
    monkeypatch.setattr(domain_history, "fetch_public", no_wayback())
    out = await DomainHistorySkill().run(state("https://new.example/p"))
    [finding] = out["findings"]
    assert finding.evidence.startswith("Common Crawl has no copies of new.example")


async def test_a_slow_down_pauses_common_crawl_and_the_archive_steps_in(monkeypatch: pytest.MonkeyPatch) -> None:
    server = crawl_server({}, fail=503)
    monkeypatch.setattr(common_crawl, "fetch_public", server)
    copies = {"20251203090101": "bakery.html"}

    async def archive(url: str, **_: Any) -> bytes:
        if "/cdx/search/cdx" in url:
            return page("cdx.json")
        timestamp = url.split("/web/", 1)[1].split("id_/", 1)[0]
        if timestamp not in copies:
            raise httpx.ReadTimeout("slow")
        return page(copies[timestamp])

    monkeypatch.setattr(domain_history, "fetch_public", archive)
    out = await DomainHistorySkill().run(state("https://www.oudebakker.example/p"))
    assert out["features"]["domain_history.changed"] == 1.0
    assert common_crawl.pace.paused_until > 0 and len(server.asked) == 1  # one request, then a pause, no retry
    with pytest.raises(common_crawl.ArchiveBusyError):
        await common_crawl.crawls()
    assert len(server.asked) == 1


async def test_neither_archive_answering_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common_crawl, "fetch_public", crawl_server({}, fail=504))
    monkeypatch.setattr(settings, "wayback_daily_cap", 0)
    with pytest.raises(common_crawl.ArchiveBusyError) as raised:
        await DomainHistorySkill().run(state("https://www.oudebakker.example/p"))
    assert str(raised.value).startswith("Common Crawl failed: HTTP 504")  # its real cause, not the archive's pace
    monkeypatch.setattr(common_crawl, "pace", common_crawl.new_pace())
    monkeypatch.setattr(common_crawl, "fetch_public", crawl_server({"CC-MAIN-2025-47": "bakery.html"}))
    out = await DomainHistorySkill().run(state("https://www.oudebakker.example/p"))
    assert out["features"]["domain_history.changed"] == 1.0


def _wayback_copies() -> Any:
    copies = {"20251203090101": "bakery.html"}

    async def archive(url: str, **_: Any) -> bytes:
        if "/cdx/search/cdx" in url:
            return page("cdx.json")
        timestamp = url.split("/web/", 1)[1].split("id_/", 1)[0]
        if timestamp not in copies:
            raise httpx.ReadTimeout("slow")
        return page(copies[timestamp])

    return archive


async def test_common_crawl_down_the_archive_answers_and_the_failure_is_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(common_crawl, "fetch_public", crawl_server({}, fail=504))
    monkeypatch.setattr(domain_history, "fetch_public", _wayback_copies())
    with capture_logs() as logs:
        found = await domain_history.history("www.oudebakker.example")
    assert found["archive"] == "wayback" and found["asked"] == ["wayback"] and found["snapshots"]
    [logged] = [e for e in logs if e["event"] == "domain_history.common_crawl_failed"]
    assert logged["cause"].startswith("HTTP 504") and logged["log_level"] == "warning"
    assert "oudebakker" not in json.dumps(logged)  # never the address


async def test_both_archives_down_names_common_crawls_cause_first(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow(url: str, **_: Any) -> bytes:
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(common_crawl, "fetch_public", slow)
    monkeypatch.setattr(domain_history, "fetch_public", crawl_server({}, fail=502))  # the archive's CDX fails
    with pytest.raises(RuntimeError) as raised:
        await domain_history.history("www.oudebakker.example")
    said = str(raised.value)
    assert said.startswith("Common Crawl failed: timed out (ReadTimeout)")
    assert said.index("Common Crawl") < said.index("The Internet Archive failed too: HTTP 502")
    assert "oudebakker" not in said


async def test_common_crawl_answering_leaves_the_archive_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = dict.fromkeys(("CC-MAIN-2026-43", "CC-MAIN-2026-34", "CC-MAIN-2025-47"), "bakery.html")
    monkeypatch.setattr(common_crawl, "fetch_public", crawl_server(pages))
    monkeypatch.setattr(domain_history, "fetch_public", no_wayback())  # raises if asked
    with capture_logs() as logs:
        found = await domain_history.history("www.oudebakker.example")
    assert found["archive"] == "commoncrawl" and found["asked"] == ["commoncrawl"] and found["snapshots"]
    assert not [e for e in logs if e["event"] == "domain_history.common_crawl_failed"]


def test_the_archives_daily_cap() -> None:
    caps = {"cap": 2}
    pace = Pace(per_minute=lambda: 100, pause_s=lambda: 900.0, daily_cap=lambda: caps["cap"])
    assert pace.take() and pace.take() and not pace.take()
    pace.day = date(2000, 1, 1)  # a new day: the cap starts again
    assert pace.take()
    assert settings.model_fields["wayback_per_minute"].default == 3
    assert settings.model_fields["wayback_daily_cap"].default == 100


# ── An address's first sighting ───────────────────────────────────────────────

# A .nl-style registry answer: the domain is known, its registration date isn't published.
NO_DATE = {"events": [{"eventAction": "last changed", "eventDate": "2024-02-01"}], "entities": []}


def registry() -> Any:
    async def fetch(url: str, **_: Any) -> bytes:
        return b"[]" if "crt.sh" in url else json.dumps(NO_DATE).encode()

    return fetch


async def test_first_copy_from_common_crawl(monkeypatch: pytest.MonkeyPatch) -> None:
    server = crawl_server({}, index="index_first.json")
    monkeypatch.setattr(common_crawl, "fetch_public", server)
    monkeypatch.setattr(domain_history, "fetch_public", no_wayback())
    monkeypatch.setattr(domain_age, "fetch_public", registry())
    out = await DomainAgeSkill().run(state("https://www.trailstone.example/p") | {"features": {}})
    f = out["features"]
    assert f["domain_age.archive_first_year"] == 2021.0 and f["domain_age.first_seen_common_crawl"] == 1.0
    assert f["domain_age.first_seen_unfret"] == 0.0
    first = next(x for x in out["findings"] if x.type == "archive_first_capture")
    assert first.evidence == (
        "Online since at least 2021-10-20, per Common Crawl (the oldest copy of trailstone.example found there; "
        "a minimum, not the registration date)."
    )
    # The crawls nearest 10, 5 and 3 years ago, all at once, a page or a redirect counting.
    probes = [u for u, _ in server.asked if "-index?" in u]
    assert {u.split("/")[3].split("-index")[0] for u in probes} == {
        "CC-MAIN-2016-44",
        "CC-MAIN-2021-43",
        "CC-MAIN-2023-50",
    }
    assert all("limit=1" in u and "status%3A%5B23%5D.." in u for u in probes)

    # "Established shop" names Common Crawl, once the look at its earlier copies agreed.
    agreed = f | {"domain_age.age_years": 0.0, "domain_history.checked": 1.0, "domain_history.snapshots": 3.0}
    verdict = shopper.build(80, features=agreed, findings=[], facts=PageFacts(), page_type="storefront")
    established = next(p for p in verdict.positives if p.type == "established_archive")
    assert established.detail.startswith(
        "This web shop's address has been online since at least 2021, per Common Crawl,"
    )
    carded = card.complete(verdict.model_dump(), state("https://www.trailstone.example/p") | {
        "features": agreed, "findings": []}, available=SKILLS, applicable=SKILLS)  # fmt: skip
    shown = next(p for p in carded["positives"] if p["type"] == "established_archive")
    assert shown["source"] == "Earlier copies of the site (Common Crawl, commoncrawl.org)"


async def test_no_common_crawl_copy_asks_the_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common_crawl, "fetch_public", crawl_server({}, index="index.json"))  # none in those crawls
    asked: list[str] = []

    async def archive(url: str, **_: Any) -> bytes:
        asked.append(url)
        return page("cdx_first.json")

    monkeypatch.setattr(domain_history, "fetch_public", archive)
    monkeypatch.setattr(domain_age, "fetch_public", registry())
    out = await DomainAgeSkill().run(state("https://www.trailstone.example/p") | {"features": {}})
    assert out["features"]["domain_age.archive_first_year"] == 2009.0
    assert out["features"]["domain_age.first_seen_common_crawl"] == 0.0 and len(asked) == 1


async def test_unfrets_own_first_sighting(monkeypatch: pytest.MonkeyPatch) -> None:
    # Neither archive has the shop, but a page Unfret fetched itself showed it before: seen since that day.
    monkeypatch.setattr(common_crawl, "fetch_public", crawl_server({}, index="index.json"))
    monkeypatch.setattr(domain_history, "fetch_public", no_wayback([]))
    monkeypatch.setattr(domain_age, "fetch_public", registry())
    store = shop_store.store()
    await store.record(shop_store.ShopRecord("trailstone.example", date(2026, 3, 2)))
    # Content a client sent says nothing about since when the shop is online.
    client = observations.Provenance(fetched=False, tier="quick").bits
    await store.record(shop_store.ShopRecord("forged.example", date(2025, 1, 1), bits=client))
    assert await store.first_fetched("forged.example") is None
    assert await store.first_fetched("trailstone.example") == date(2026, 3, 2)

    out = await DomainAgeSkill().run(state("https://www.trailstone.example/p") | {"features": {}})
    f = out["features"]
    assert f["domain_age.first_seen_unfret"] == 1.0 and f["domain_age.archive_first_year"] == 2026.0
    first = next(x for x in out["findings"] if x.type == "archive_first_capture")
    assert first.evidence.startswith("Online since at least 2026-03-02, per Unfret's own checks (the first time")
    assert shopper.seen_by(f) == "Unfret's own checks"

    # An archive's older copy wins over Unfret's own sighting.
    monkeypatch.setattr(fact_cache, "_store", fact_cache.MemoryStore())
    monkeypatch.setattr(common_crawl, "fetch_public", crawl_server({}, index="index_first.json"))
    out = await DomainAgeSkill().run(state("https://www.trailstone.example/p") | {"features": {}})
    assert out["features"]["domain_age.first_seen_common_crawl"] == 1.0
    assert out["features"]["domain_age.archive_first_year"] == 2021.0


async def test_past_the_write_limit_a_fetched_shop_still_gets_its_first_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fingerprints", True)
    facts = PageFacts(platform="shopify")
    analysis = state("https://www.lantern.example/p") | {"facts": facts}
    token = observations._current.set(observations.Provenance(fetched=True, tier="quick", record=False))
    try:
        shop_store.observe_later(analysis)
        await shop_store.drain()
    finally:
        observations._current.reset(token)
    assert await shop_store.store().first_fetched("lantern.example") == shop_store.today()
    assert await shop_store.store().fingerprint("lantern.example") is None  # nothing else was recorded
    # A client's page past the limit records nothing at all.
    token = observations._current.set(observations.Provenance(fetched=False, tier="quick", record=False))
    try:
        shop_store.observe_later(state("https://other.example/p") | {"facts": facts})
        await shop_store.drain()
    finally:
        observations._current.reset(token)
    assert await shop_store.store().first_fetched("other.example") is None
