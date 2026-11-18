"""The /accuracy page and the script behind its numbers: aggregates only, every number filled, the right scorer."""

import copy
import json
import re
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from evals.accuracy import (
    DEFAULT_OUT,
    HISTORY_OUT,
    LABEL_ORIGINS,
    TOLERATED_MISSING,
    band_summary,
    check_aggregates_only,
    check_history,
    complete,
    coverage,
    kind,
    label_origin,
    missing_skills,
    publish,
    record,
)
from evals.corpus import LabelRecord
from evals.metrics import Outcome
from unfret import accuracy
from unfret.main import app
from unfret.routes.pages import PARTIALS_DIR, STATIC_DIR, asset_tag, template
from unfret.scoring import load_model


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


def _summary() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(accuracy.ACCURACY_PATH.read_text(encoding="utf-8"))
    return data


def test_the_script_writes_the_file_the_page_reads() -> None:
    assert DEFAULT_OUT == accuracy.ACCURACY_PATH


def test_the_published_numbers_are_aggregates_only() -> None:
    data = _summary()
    check_aggregates_only(data)  # no URL, domain, e-mail or page id anywhere

    def leaves(value: object) -> list[object]:
        if isinstance(value, dict):
            return [leaf for v in value.values() for leaf in leaves(v)]
        if isinstance(value, list):
            return [leaf for v in value for leaf in leaves(v)]
        return [value]

    strings = [v for v in leaves(data) if isinstance(v, str)]
    # Only the date, the scorer's version, the language model's name and the replayed split: no names, records or text.
    assert strings == [data["generated"], data["scorer_version"], data["llm_model"], "all"], strings


@pytest.mark.parametrize(
    "text",
    ["https://shop.example/p/1", "see www.example", "fake-shop.com", "a@b", "page mnd-0123456789ab"],
)
def test_a_summary_with_anything_from_a_page_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="aggregates only"):
        check_aggregates_only({"groups": {"products": {"note": text}}})


def test_the_numbers_are_for_the_scorer_this_repository_ships() -> None:
    # A new model.json needs `make accuracy` (or a note on the page), not numbers measured with its predecessor.
    model = load_model()
    assert model is not None
    assert _summary()["scorer_version"] == model.version


def test_every_dataset_has_a_label_origin_the_page_can_name() -> None:
    assert set(LABEL_ORIGINS.values()) <= set(accuracy.ORIGIN_NAMES)
    with pytest.raises(ValueError, match="LABEL_ORIGINS"):
        label_origin(LabelRecord(id="x-1", label="legit", source="dataset", origin_dataset="new-dataset"))


def test_kinds_of_page() -> None:
    def record(page_type: str) -> LabelRecord:
        return LabelRecord(id="x", label="legit", source="dataset", origin_dataset="d", page_type=page_type)

    assert kind(record("storefront")) == kind(record("")) == "shop"
    assert kind(record("aggregate_listing")) == "marketplace_product"
    assert kind(record("p2p_listing")) == "ad"
    assert kind(record("service_site")) == "trade_site"
    assert kind(record("service_listing")) == "trade_profile"


def _outcome(label: str, band: str, *, note: str = "", error: str = "", score: int = 50) -> Outcome:
    return Outcome(id=f"{label}-{band}", label=label, score=score, band=band, note=note, error=error)


def test_band_summary_precision_and_catch_rate() -> None:
    outcomes = [
        _outcome("legit", "reliable"),
        _outcome("legit", "reliable"),
        _outcome("scam", "reliable"),
        _outcome("legit", "concerns"),
        _outcome("junk", "concerns"),
        _outcome("legit", "careful"),
        _outcome("scam", "careful"),
        _outcome("scam", "junk_or_scam"),
        _outcome("scam", "junk_or_scam"),
        # Not counted: a check couldn't be replayed, the replay failed, or the page isn't one Unfret assesses.
        _outcome("scam", "junk_or_scam", note="1 skill(s) unavailable: content"),
        _outcome("scam", "", error="CacheMissError: …", score=0),
        _outcome("legit", "", score=-1),
    ]
    summary = band_summary(outcomes)

    assert (summary["scored"], summary["unreliable"], summary["legit"]) == (9, 5, 4)
    assert summary["bands"]["reliable"] == {
        "pages": 3,
        "unreliable": 1,
        "legit": 2,
        "unreliable_share": 0.333,
        "share_of_unreliable": 0.2,
        "share_of_legit": 0.5,
    }
    assert summary["bands"]["junk_or_scam"]["unreliable_share"] == 1.0
    assert summary["caught"] == 0.6  # 3 of 5 unreliable pages at Think twice or Serious concerns
    assert summary["caught_with_worth_a_look"] == 0.8
    assert summary["warning_precision"] == 0.75
    assert summary["legit_warned"] == 0.25
    assert coverage(outcomes) == {
        "labelled": 12,
        "scored": 9,
        "not_assessed": 1,
        "missing_recordings": 2,
        "without_llm_read": 0,
    }
    assert not complete(outcomes[9])


def test_ads_and_trades_may_count_without_the_llm_read() -> None:
    without_read = _outcome("scam", "careful", note="1 skill(s) unavailable: content")
    without_reviews = _outcome("legit", "reliable", note="2 skill(s) unavailable: content, reviews")
    unnamed = _outcome("legit", "reliable", note="1 skill(s) unavailable")
    tolerated = frozenset({"content"})

    assert missing_skills(without_reviews) == {"content", "reviews"}
    assert missing_skills(unnamed) == {"?"}
    assert complete(without_read, tolerated) and not complete(without_read)
    assert not complete(without_reviews, tolerated) and not complete(unnamed, tolerated)
    assert coverage([without_read, without_reviews, unnamed], tolerated) == {
        "labelled": 3,
        "scored": 1,
        "not_assessed": 0,
        "missing_recordings": 2,
        "without_llm_read": 1,
    }
    assert TOLERATED_MISSING["products"] == frozenset()  # the product scorer leans on the LLM read


@pytest.mark.parametrize(
    ("value", "text"),
    [(0.818, "82%"), (0.069, "6.9%"), (0.0, "0%"), (1.0, "100%"), (0.996, "99.6%"), (None, "not measured")],
)
def test_percentages(value: float | None, text: str) -> None:
    assert accuracy.pct(value) == text


def test_every_placeholder_is_filled_and_every_headline_number_measured() -> None:
    page = accuracy.render(template("accuracy.html"))

    assert "{{" not in page and "}}" not in page
    head, _, _ = page.partition('id="ads"')
    assert accuracy.NOT_MEASURED not in head  # products and false alarms are fully measured
    for label in ("Looks fine", "Worth a look", "Think twice", "Serious concerns"):
        assert label in page


def test_dates_are_written_out() -> None:
    assert accuracy.render("{{date:generated}}", {"generated": "2026-11-07"}) == "November 7, 2026"
    assert accuracy.long_date(None) == "not measured"


def test_a_group_without_numbers_says_so() -> None:
    assert "Not measured" in accuracy.bands_table({"groups": {}}, "trades")
    assert accuracy.render("{{pct:groups.trades.caught}}", {}) == "not measured"


def test_missing_recordings_are_named_not_hidden() -> None:
    data = {
        "groups": {
            "trades": {
                "scored": 10,
                "unreliable": 4,
                "legit": 6,
                "coverage": {
                    "labelled": 40,
                    "scored": 10,
                    "not_assessed": 10,
                    "missing_recordings": 20,
                    "without_llm_read": 10,
                },
                "missing_by_label_origin": {"established_business": 20},
            }
        }
    }
    note = accuracy.coverage_note(data, "trades")
    assert "20 of the 40 labelled pages couldn't be measured" in note
    assert "long-established businesses" in note
    assert "10 of them were measured without the language model's reading" in note
    assert "10 more were set aside" in note


def test_a_different_running_scorer_is_said() -> None:
    assert "scorer this server runs" in accuracy.version_note(_summary())
    assert "aren't published here yet" in accuracy.version_note({"scorer_version": "v0-old"})


async def test_the_page_is_served_with_the_strict_policy(client: httpx.AsyncClient) -> None:
    response = await client.get("/accuracy")

    assert response.status_code == 200
    assert "default-src 'none'" in response.headers["content-security-policy"]
    text = " ".join(response.text.split())
    assert "How accurate is Unfret?" in text
    # The numbers are in the page, no script needed: the only one is the footer's (an affiliate link's code, ref.js).
    assert response.text.count("<script") == 1 and "/ref.js" in response.text
    for section in ("method", "false-alarms", "ads", "trades", "limits", "changelog"):
        assert f'id="{section}"' in response.text


def test_the_page_is_linked_from_the_footer_and_the_developers_page() -> None:
    assert 'href="/accuracy"' in (PARTIALS_DIR / "footer.html").read_text(encoding="utf-8")
    assert 'href="/accuracy"' in (STATIC_DIR / "developers.html").read_text(encoding="utf-8")


def test_the_changelog_names_the_running_scorer() -> None:
    model = load_model()
    assert model is not None
    assert model.version in (STATIC_DIR / "accuracy.html").read_text(encoding="utf-8")


async def test_the_numbers_are_served_as_json(client: httpx.AsyncClient) -> None:
    response = await client.get("/static/accuracy.json")

    assert response.status_code == 200
    assert response.json() == _summary()


# ── The retrained scorer unfret.net runs, next to the open baseline, and every earlier version ─────────────────


def _service() -> dict[str, Any]:
    data = accuracy.service_summary()
    assert data is not None, "the numbers of the scorer unfret.net runs are published"
    return data


def _history_file() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(accuracy.HISTORY_PATH.read_text(encoding="utf-8"))
    return data


@pytest.fixture
def running(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Pretend this server runs a given scorer version."""

    def run(version: str | None) -> None:
        monkeypatch.setattr(accuracy, "load_model", lambda: SimpleNamespace(version=version) if version else None)

    return run


def test_the_history_is_checked_aggregates_for_every_published_version() -> None:
    history = _history_file()
    assert HISTORY_OUT == accuracy.HISTORY_PATH
    check_history(history)  # fixed fields, checked summaries, metrics as their summaries give them
    entries = history["entries"]
    assert entries == accuracy.newest_first(entries)  # stored newest first
    service = _service()
    assert service["scorer_version"] != _summary()["scorer_version"]
    # The open baseline's entry is accuracy.json itself, for the scorer this repository ships.
    baseline = accuracy.baseline_entry()
    assert baseline["summary"] == _summary() and baseline["kind"] == "public" and baseline["released"]
    # Measured on the same replay as the baseline, so the side-by-side table compares like with like.
    assert service["corpus"] == _summary()["corpus"]
    assert service["scorer_version"] in (STATIC_DIR / "accuracy.html").read_text(encoding="utf-8")  # the changelog


def test_the_single_service_file_is_gone_and_nothing_points_at_it() -> None:
    assert not (STATIC_DIR / "accuracy-service.json").exists()
    tracked = subprocess.run(
        ["git", "grep", "-l", "accuracy-service", "--", ":!CHANGELOG.md", ":!tests/unit/test_accuracy.py"],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).parents[2],
    )
    assert tracked.stdout == "", tracked.stdout


@pytest.mark.parametrize(
    ("version", "lead", "name", "note"),
    [
        ("service", "service", "the retrained scorer unfret.net runs", "It is the scorer this server runs."),
        ("baseline", "baseline", "the open baseline", "It is the scorer this server runs."),
        ("v9-own", "baseline", "the open baseline", "aren't published here yet"),
    ],
)
def test_the_page_leads_with_the_numbers_of_the_scorer_this_server_runs(
    running: Any, version: str, lead: str, name: str, note: str
) -> None:
    versions = {"service": _service()["scorer_version"], "baseline": _summary()["scorer_version"]}
    running(versions.get(version, version))
    assert accuracy.page_summary()["scorer_version"] == versions[lead]
    page = " ".join(accuracy.render(template("accuracy.html")).split())
    assert f"with scorer <code>{versions[lead]}</code>, {name}" in page
    assert note in page
    if version == "v9-own":
        assert "This server runs scorer <code>v9-own</code>" in page
    # Both scorers' numbers are on the page either way, each named.
    table = page.split('id="scorers"', 1)[1].split("</table>", 1)[0]
    assert versions["service"] in table and versions["baseline"] in table
    assert "Retrained, runs on unfret.net" in table and "Open baseline, self-hosted" in table
    assert table.count("(this server)") == (0 if version == "v9-own" else 1)
    assert accuracy.pct(_service()["groups"]["products"]["caught"]) in table


def test_without_published_service_numbers_the_baseline_leads_alone(
    running: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(accuracy, "history", list)
    running("v1-whatever")
    assert accuracy.page_summary() == _summary()
    table = accuracy.scorers_table()
    assert "Open baseline" in table and "Retrained" not in table
    assert "Release date not recorded" in table  # the baseline without a history entry


# Made-up versions, each with its own numbers and corpus size, to render the history with.


def _made_up(version: str, measured: str, pages: int, caught: float) -> dict[str, Any]:
    summary = copy.deepcopy(_summary())
    summary.update(scorer_version=version, generated=measured)
    summary["corpus"]["pages"] = pages
    summary["groups"]["products"]["caught"] = caught
    return summary


def _baseline(released: str = "2026-01-10") -> dict[str, Any]:
    return accuracy.entry(_summary(), "public", released)


ONE = [
    ("v7-20260301-C", "private", "2026-03-02", "2026-03-01", 3000, 0.81),
]
THREE = [
    ("v7-20260201-C", "private", "2026-02-03", "2026-02-01", 2200, 0.71),
    ("v7-20260401-C", "private", "2026-04-02", "2026-04-01", 3400, 0.88),  # the newest
    ("v7-20260301-C", "private", "2026-03-02", "2026-03-01", 3000, 0.81),
]


def _entries(rows: list[tuple[str, str, str, str, int, float]]) -> list[dict[str, Any]]:
    return [
        accuracy.entry(_made_up(v, measured, pages, caught), k, released)
        for v, k, released, measured, pages, caught in rows
    ]


@pytest.fixture
def history(monkeypatch: pytest.MonkeyPatch, running: Any) -> Any:
    """Render the page with a made-up history (plus the real open baseline), on a server running the newest."""

    def use(entries: list[dict[str, Any]]) -> str:
        ordered = accuracy.newest_first(entries)
        monkeypatch.setattr(accuracy, "history", lambda: ordered)
        running(next(e["version"] for e in ordered if e["kind"] == "private"))
        return accuracy.render(template("accuracy.html"))

    return use


def _boxes(page: str) -> list[str]:
    return re.findall(r"<details\b[^>]*>.*?</details>", page, re.DOTALL)


def _top(page: str) -> str:
    return page.split('id="scorers"', 1)[1].split("</table>", 1)[0]


def test_with_one_retrained_scorer_the_history_says_it_is_the_first(history: Any) -> None:
    page = history([*_entries(ONE), _baseline()])
    top = _top(page)
    assert "v7-20260301-C" in top and _summary()["scorer_version"] in top
    assert "Released March 2, 2026, measured on 3,000 labelled pages" in top
    assert "Released January 10, 2026, measured on 2,641 labelled pages" in top
    assert _boxes(page) == []
    assert "This is the first retrained scorer" in page
    assert "aren't strictly comparable" in page


def test_with_three_the_newest_leads_and_the_others_fold_below_newest_first(history: Any) -> None:
    older_baseline = accuracy.entry(_made_up("v0-20251201-C", "2025-12-01", 1500, 0.6), "public", "2025-12-02")
    page = history([*_entries(THREE), _baseline(), older_baseline])
    top = _top(page)
    # The top: the newest retrained scorer and the open baseline this repository ships, nothing else.
    assert "v7-20260401-C" in top and _summary()["scorer_version"] in top and "88%" in top
    assert "v7-20260301-C" not in top and "v7-20260201-C" not in top and "v0-20251201-C" not in top
    boxes = _boxes(page)
    versions = [re.search(r"<code>([^<]+)</code>", b).group(1) for b in boxes]  # type: ignore[union-attr]
    assert versions == ["v7-20260301-C", "v7-20260201-C", "v0-20251201-C"]  # newest first, the old baseline too
    assert "This is the first" not in page
    first, second, baseline = boxes
    assert "Retrained scorer <code>v7-20260301-C</code>" in first
    assert "Released March 2, 2026, measured on 3,000 labelled pages</span></span></summary>" in first
    assert "on 3,000 labelled pages" in first and "81%" in first
    assert "Released February 3, 2026" in second and "on 2,200 labelled pages" in second and "71%" in second
    assert "Open baseline <code>v0-20251201-C</code>" in baseline and "Released December 2, 2025" in baseline
    # The same numbers as the table above, under the same headings.
    for heading, _ in accuracy._columns():
        assert all(f'<th scope="row">{heading}</th>' in b for b in boxes), heading


def test_the_boxes_are_closed_and_work_without_a_script(history: Any) -> None:
    page = history([*_entries(THREE), _baseline()])
    opening = re.findall(r"<details\b[^>]*>", page)
    assert len(opening) == 2 and not any(re.search(r"\bopen\b", tag) for tag in opening)
    for box in _boxes(page):
        assert box.count("<summary>") == 1 and "<script" not in box and " style=" not in box
        assert not re.search(r"\son[a-z]+=", box)


async def test_the_page_has_the_strict_policy_and_loads_nothing_from_elsewhere(client: httpx.AsyncClient) -> None:
    response = await client.get("/accuracy")
    csp = response.headers["content-security-policy"]
    for directive in ("default-src 'none'", "script-src 'self'", "style-src 'self'", "frame-ancestors 'none'"):
        assert directive in csp, directive
    assert "unsafe-inline" not in csp
    page = response.text
    for tag in re.findall(r"<(?:script|img|link)\b[^>]*>", page):
        address = re.search(r'\b(?:src|href)="([^"]*)"', tag)
        assert address and address.group(1).startswith(f"/static/{asset_tag()}/"), tag
    assert "<style" not in page and " style=" not in page and "<iframe" not in page
    assert not re.search(r"\son[a-z]+=", page)
    assert 'id="history"' in page


def _store(tmp_path: Path, version: str, summary: dict[str, Any]) -> Path:
    (tmp_path / version).mkdir()
    (tmp_path / version / "accuracy.json").write_text(json.dumps(summary))
    return tmp_path


def test_publishing_adds_a_retrained_scorers_entry_after_checking_it(tmp_path: Path) -> None:
    out = tmp_path / "history.json"
    record(_summary(), "public", "2026-01-10", out)
    summary = {**_summary(), "scorer_version": "v2-test-C"}
    publish("v2-test-C", root=_store(tmp_path, "v2-test-C", summary), out=out, released="2026-02-01")
    entries = json.loads(out.read_text())["entries"]
    assert [(e["version"], e["kind"], e["released"]) for e in entries] == [
        ("v2-test-C", "private", "2026-02-01"),
        (_summary()["scorer_version"], "public", "2026-01-10"),
    ]
    assert entries[0]["summary"] == summary and entries[0]["corpus_pages"] == summary["corpus"]["pages"]
    assert entries[0]["metrics"] == accuracy.metrics(summary)
    # Publishing it again (measured anew) replaces its entry and keeps its release date.
    remeasured = {**summary, "generated": "2026-03-01"}
    (tmp_path / "v2-test-C" / "accuracy.json").write_text(json.dumps(remeasured))
    publish("v2-test-C", root=tmp_path, out=out)
    entries = json.loads(out.read_text())["entries"]
    assert len(entries) == 2 and entries[0]["measured"] == "2026-03-01" and entries[0]["released"] == "2026-02-01"
    with pytest.raises(FileNotFoundError, match="make accuracy"):
        publish("v3-missing-C", root=tmp_path, out=out)


@pytest.mark.parametrize(
    "change",
    [
        {"scorer_version": "v2-other-C"},  # another version's numbers
        {"note": "a shop's own words"},  # any text beyond the fixed fields
        {"groups": {"Some Shop": {"pages": 1}}},  # a name as a key
        {"llm_model": "see www.example"},
    ],
)
def test_publishing_refuses_anything_but_aggregates_for_that_version(tmp_path: Path, change: dict[str, Any]) -> None:
    summary = {**_summary(), "scorer_version": "v2-test-C", **change}
    with pytest.raises(ValueError, match="aggregates only|not 'v2-test-C'"):
        publish("v2-test-C", root=_store(tmp_path, "v2-test-C", summary), out=tmp_path / "history.json")
    assert not (tmp_path / "history.json").exists()


def _tampered(change: Any) -> dict[str, Any]:
    history = copy.deepcopy(_history_file())
    change(history["entries"][0])
    return history


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda e: e.update(kind="secret"), "kind"),
        (lambda e: e.update(released="18 November"), "ISO date"),
        (lambda e: e.update(shop="Some Shop"), "exactly the fields"),
        (lambda e: e["metrics"].update(caught=0.99), "metrics isn't what its summary gives"),
        (lambda e: e.update(corpus_pages=10), "corpus_pages isn't"),
        (lambda e: e["summary"].update(note="a page's words"), "aggregates only"),
        (lambda e: e.update(version="v0-other-C"), "not 'v0-other-C'"),
    ],
)
def test_the_history_refuses_anything_but_checked_entries(change: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_history(_tampered(change))


def test_the_history_refuses_a_version_twice() -> None:
    history = _history_file()
    with pytest.raises(ValueError, match="twice"):
        check_history({"entries": [history["entries"][0], history["entries"][0]]})
    with pytest.raises(ValueError, match="entries"):
        check_history({"entries": [], "extra": 1})


async def test_the_history_is_served_as_json(client: httpx.AsyncClient) -> None:
    response = await client.get("/static/accuracy-history.json")

    assert response.status_code == 200
    assert response.json() == _history_file()
    assert (await client.get("/static/accuracy-service.json")).status_code == 404


def test_a_server_running_an_earlier_retrained_scorer_leads_with_its_numbers(history: Any, running: Any) -> None:
    history([*_entries(THREE), _baseline()])
    running("v7-20260301-C")
    assert accuracy.page_summary()["scorer_version"] == "v7-20260301-C"
    page = " ".join(accuracy.render(template("accuracy.html")).split())
    assert (
        "with scorer <code>v7-20260301-C</code>, an earlier retrained scorer. It is the scorer this server runs."
        in page
    )
    assert "(this server)" not in _top(page)  # the table still shows the newest and the baseline
