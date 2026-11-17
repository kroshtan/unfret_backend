"""A result in public wording (what a shared result shows): about the listing, never "scam", no score."""

import json
from typing import Any

import pytest

from unfret import public_result, shopper


def response() -> dict[str, Any]:
    return {
        "score": 15,
        "explanation": "A scam shop.",
        "verdict": {
            "band": "junk_or_scam",
            "label": "Many warning signs",
            "headline": "This is a scam.",
            "reasons": [
                {
                    "type": "new_domain",
                    "title": "A brand-new shop",
                    "detail": "Registered 3 weeks ago.",
                    "severity": "high",
                },
                {"type": "pushy_copy", "title": "Pushy sales talk", "detail": '"Not a SCAM!"', "severity": "medium"},
                {"type": "x", "title": "Fraudulent?", "detail": "", "severity": "low"},
            ],
            "positives": [{"type": "vat_registered", "title": "A valid VAT number", "detail": "NL123"}],
        },
        "assessment": {
            "risk_score": 0.9,
            "confidence": 0.75,
            "model_version": "v7",
            "flags": [
                {"type": "content.scam_copy", "label": "Pushy sales talk", "evidence": "scam", "weight": 1.2},
                {"type": "domain_age.age_years", "label": "Domain age", "evidence": "0.1", "kind": "context"},
            ],
        },
    }


@pytest.mark.parametrize(
    ("band", "public", "symbol", "label"),
    [
        ("reliable", "reliable", "✓", "Unfret found nothing worrying on this listing"),
        ("concerns", "concerns", "!", "Unfret would suggest a closer look at this listing"),
        ("careful", "careful", "⚠", "Unfret would point out warning signs on this listing"),
        ("junk_or_scam", "warning", "✕", "Unfret would warn you about this listing"),
    ],
)
def test_each_band_describes_the_listing(band: str, public: str, symbol: str, label: str) -> None:
    result = public_result.public_result({"score": 50, "verdict": {"band": band, "headline": "Fine.", "reasons": []}})
    assert result["verdict"] | {"reasons": [], "positives": []} == {
        "band": public,
        "label": label,
        "symbol": symbol,
        "headline": "Fine.",
        "reasons": [],
        "positives": [],
    }
    assert "scam" not in json.dumps(result).lower()


def test_the_public_result_never_says_scam_and_shows_no_score() -> None:
    left_out: list[str] = []
    result = public_result.public_result(response(), left_out)

    text = json.dumps(result)
    assert "scam" not in text.lower() and "fraud" not in text.lower()
    assert "score" not in text and "explanation" not in text and "risk" not in text
    verdict = result["verdict"]
    assert verdict["headline"] == shopper.catalogue()["headlines"]["junk_or_scam"]  # the catalogue's for the band
    assert [r["title"] for r in verdict["reasons"]] == ["A brand-new shop", "Pushy sales talk"]
    assert verdict["reasons"][1]["detail"] == ""  # the quote is left out, the finding stays
    assert verdict["positives"] == [{"title": "A valid VAT number", "detail": "NL123"}]
    assert result["assessment"]["flags"][0] == {"label": "Pushy sales talk", "evidence": "", "kind": "signal"}
    assert left_out == [
        "headline",
        "detail evidence: Pushy sales talk",
        "reason detail: Pushy sales talk",
        "reason x",
    ]


def test_a_band_without_a_verdict_follows_the_score() -> None:
    assert public_result.public_result({"score": 90})["band"] == "reliable"
    assert public_result.public_result({"score": 5})["band"] == "warning"


def test_urls_lose_their_fragment_and_must_be_web_addresses() -> None:
    assert public_result.clean_url(" https://Shop.example/p?id=1#reviews ") == "https://Shop.example/p?id=1"
    assert public_result.host_of("https://www.Shop.example/p") == "shop.example"
    with pytest.raises(ValueError, match="http"):
        public_result.clean_url("javascript:alert(1)")
