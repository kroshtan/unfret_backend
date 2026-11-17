"""The /accuracy page and the script behind its numbers: aggregates only, every number filled, the right scorer."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from evals.accuracy import (
    DEFAULT_OUT,
    LABEL_ORIGINS,
    TOLERATED_MISSING,
    band_summary,
    check_aggregates_only,
    complete,
    coverage,
    kind,
    label_origin,
    missing_skills,
)
from evals.corpus import LabelRecord
from evals.metrics import Outcome
from unfret import accuracy
from unfret.main import app
from unfret.routes.pages import PARTIALS_DIR, STATIC_DIR, template
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
    assert accuracy.render("{{date:generated}}", {"generated": "2026-11-07"}) == "7 November 2026"
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
    assert "<script" not in response.text  # the numbers are in the page, no script needed
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
