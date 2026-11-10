"""Shared results: the share token, what a share makes public, and its page."""

import base64
import json
from datetime import UTC, date, datetime
from typing import Any

import pytest

from unfret import shares
from unfret.routes.pages import STATIC_DIR
from unfret.routes.shares import render_share_page

SECRET = "test-secret"
TTL = 7 * 24 * 3600
NOW = datetime(2026, 11, 9, 14, 30, tzinfo=UTC).timestamp()
URL = "https://shop.example/p/led-mask?id=7&utm_source=mail&fbclid=abc#reviews"


def response(**extra: Any) -> dict[str, Any]:
    return {
        "score": 15,
        "explanation": "A scam shop.",
        "verdict": {
            "band": "junk_or_scam",
            "label": "Many warning signs",
            "headline": "Many warning signs: a discount it never had.",
            "reasons": [
                {
                    "type": "was_never_charged",
                    "title": "'Was' price not charged recently",
                    "detail": "This product sold for €19 in the past 30 days, yet the 'was' price is €199.",
                    "severity": "high",
                    "thumbnails": [],
                },
                {
                    "type": "generic_brand",
                    "title": "Unknown brand",
                    "detail": "“GLOWPRO” isn't a recognizable brand.",
                    "severity": "medium",
                    "thumbnails": [],
                },
            ],
            "positives": [{"type": "us_business", "title": "Company details", "detail": 'Run by "Glow BV".'}],
            "language": "en",
        },
        "assessment": {
            "risk_score": 0.9,
            "confidence": 0.75,
            "model_version": "v7",
            "flags": [
                {"type": "content.pushy", "label": "Pushy sales talk", "evidence": "only 2 left!", "kind": "signal"},
                {"type": "domain_age.age_years", "label": "Domain age", "evidence": "0.1", "kind": "context"},
            ],
        },
        **extra,
    }


def token(**overrides: Any) -> str:
    args: dict[str, Any] = {
        "url": URL,
        "title": "LED   mask",
        "response": response(),
        "checkout": False,
        "ttl_s": TTL,
        "now": NOW,
    }
    issued = shares.issue_token(SECRET, **(args | overrides))
    assert issued is not None
    return issued


def check(value: str, url: str = URL, result: dict[str, Any] | None = None, now: float = NOW) -> shares.Claim:
    return shares.check_token(SECRET, value, url=url, response=result or response(), ttl_s=TTL, now=now)


def test_a_token_vouches_for_the_address_title_and_day_and_names_no_account() -> None:
    issued = token()
    claim = check(issued)

    assert claim == shares.Claim("https://shop.example/p/led-mask?id=7", "LED mask", date(2026, 11, 9))
    payload = base64.urlsafe_b64decode(issued.split(".")[0] + "==").decode()
    assert "shop.example" not in payload  # the address is in the MAC, not the token
    # The same address with other tracking parameters or fragment is the same product.
    assert check(issued, url="https://shop.example/p/led-mask?id=7&gclid=x").product_url == claim.product_url


@pytest.mark.parametrize(
    "tamper",
    [
        lambda t: t[:-1] + ("0" if t[-1] != "0" else "1"),  # the MAC
        lambda t: (
            base64.urlsafe_b64encode(  # the payload (another title), with the old MAC
                base64.urlsafe_b64decode(t.split(".")[0] + "==").replace(b"LED mask", b"Free iPhone")
            )
            .decode()
            .rstrip("=")
            + "."
            + t.split(".")[1]
        ),
        lambda t: "not-a-token",
        lambda t: "%%%.zz",
        lambda t: t.split(".")[0] + ".ü",
    ],
)
def test_a_forged_token_is_refused(tamper: Any) -> None:
    with pytest.raises(shares.ShareError) as refused:
        check(tamper(token()))
    assert refused.value.code == "BAD_SHARE_TOKEN"


def test_another_secret_address_or_result_is_refused() -> None:
    issued = token()
    with pytest.raises(shares.ShareError, match="match"):
        shares.check_token("other", issued, url=URL, response=response(), ttl_s=TTL, now=NOW)
    with pytest.raises(shares.ShareError, match="match"):
        check(issued, url="https://other.example/p/led-mask?id=7")
    with pytest.raises(shares.ShareError, match="match"):
        check(issued, url="https://shop.example/p/led-mask?id=8")
    friendlier = response()
    friendlier["verdict"]["band"] = "reliable"
    with pytest.raises(shares.ShareError, match="match"):
        check(issued, result=friendlier)
    fewer = response()
    fewer["verdict"]["reasons"].pop()
    with pytest.raises(shares.ShareError, match="match"):
        check(issued, result=fewer)


def test_what_isnt_shown_may_differ_and_numbers_may_be_written_differently() -> None:
    issued = token(response=response(assessment={**response()["assessment"], "confidence": 1.0}))
    # A browser sends 1.0 back as 1; the score and explanation aren't public, so they aren't vouched for.
    check(issued, result=response(score=16, explanation="x", assessment={**response()["assessment"], "confidence": 1}))


def test_an_old_token_is_refused() -> None:
    issued = token()
    check(issued, now=NOW + TTL)
    with pytest.raises(shares.ShareError) as refused:
        check(issued, now=NOW + TTL + 1)
    assert refused.value.code == "SHARE_TOKEN_EXPIRED"


def test_a_check_with_the_checkout_page_is_never_shared() -> None:
    with pytest.raises(shares.ShareError) as refused:
        check(token(checkout=True))
    assert refused.value.code == "CHECKOUT_NOT_SHAREABLE"


@pytest.mark.parametrize(
    ("secret", "url", "score"),
    [("", URL, 50), (SECRET, URL, -1), (SECRET, "", 50), (SECRET, "ftp://shop.example/x", 50)],
)
def test_no_token_without_a_secret_an_assessment_or_a_web_address(secret: str, url: str, score: int) -> None:
    assert shares.issue_token(secret, url=url, title="", response={"score": score}, checkout=False, ttl_s=TTL) is None


def test_the_address_loses_fragment_credentials_and_tracking() -> None:
    assert shares.share_url("https://me:pw@Shop.Example:8443/a b?utm_x=1&q=lamp&ref=x#top") == (
        "https://shop.example:8443/a b?q=lamp"
    )
    with pytest.raises(shares.ShareError):
        shares.share_url("javascript:alert(1)")


def test_a_share_shows_no_score_and_none_of_the_pages_own_words() -> None:
    shown = shares.public_share_result(response())
    text = json.dumps(shown)

    assert "score" not in text and "explanation" not in text and "risk" not in text
    assert "scam" not in text.lower()
    verdict = shown["verdict"]
    assert verdict["label"] == "Unfret would warn you about this listing" and verdict["symbol"] == "✕"
    reasons = {r["title"]: r["detail"] for r in verdict["reasons"]}
    assert reasons == {
        "'Was' price not charged recently": response()["verdict"]["reasons"][0]["detail"],  # no quote: kept
        "Unknown brand": "",  # it quoted the page: the title stays
    }
    assert verdict["positives"] == [{"title": "Company details", "detail": ""}]
    assert [f["label"] for f in shown["assessment"]["flags"]] == ["Pushy sales talk", "Domain age"]
    assert all(f["evidence"] == "" for f in shown["assessment"]["flags"])
    assert "GLOWPRO" not in text and "Glow BV" not in text and "only 2 left" not in text


def test_a_headline_quoting_the_page_becomes_the_bands_own() -> None:
    quoted = response()
    quoted["verdict"]["headline"] = 'The page promises "results in 3 days".'
    headline = shares.public_share_result(quoted)["verdict"]["headline"]
    assert headline.startswith("Many warning signs: ") and "results" not in headline


def test_a_title_with_a_forbidden_word_shows_the_shop() -> None:
    assert shares.shown_title("Not a scam mask", "shop.example") == "shop.example"
    assert shares.shown_title("   ", "shop.example") == "shop.example"
    assert shares.shown_title("LED\n mask", "shop.example") == "LED mask"


def test_an_address_with_a_forbidden_word_cant_be_shared() -> None:
    claim = shares.Claim("https://scam-shop.example/p", "Mask", date(2026, 11, 9))
    with pytest.raises(shares.ShareError) as refused:
        shares.preview(claim, response())
    assert refused.value.code == "NOT_SHAREABLE"


def view(**row: Any) -> dict[str, Any]:
    claim = shares.Claim("https://shop.example/p?id=1&x=2", "LED mask", date(2026, 11, 9))
    base = shares.preview(claim, response())
    stored = {
        "id": "a" * 26,
        "expires_at": datetime(2027, 2, 7, tzinfo=UTC),
        "title": base["title"],
        "host": base["host"],
        "product_url": base["product_url"],
        "checked_on": base["checked_on"],
        "result": base["result"],
        "hidden_reason": None,
    }
    return shares.public_view(stored | row)


def test_the_page_says_who_checked_it_only_as_an_unfret_user_and_isnt_indexed() -> None:
    page = render_share_page(view())

    assert "Checked by an Unfret user on 9 November 2026; the page may have changed." in page
    assert '<meta name="robots" content="noindex, nofollow">' in page
    assert 'href="https://shop.example/p?id=1&amp;x=2" rel="nofollow noopener noreferrer"' in page
    assert "Unfret would warn you about this listing" in page
    assert "scam" not in page.lower()


def test_the_page_escapes_html_in_titles_and_reasons() -> None:
    result = shares.public_share_result(response())
    result["verdict"]["reasons"][0]["title"] = "</script><img src=x onerror=alert(1)>"
    result["verdict"]["headline"] = "<b>bold</b> {{data}}"
    page = render_share_page(view(title="</script><script>alert(1)</script>{{robots}}", result=result))

    assert "<script>alert(1)" not in page and "<img src=x" not in page and "<b>bold" not in page
    assert page.count("</script>") == 2  # the data block's and the module script's own
    assert "&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;{{robots}}" in page  # no placeholder from a title
    assert "\\u003c/script\\u003e\\u003cimg src=x" in page  # the data block can't close its element


def test_the_page_template_and_script_never_say_scam() -> None:
    for name in ("shared.html", "shared.js"):
        assert "scam" not in (STATIC_DIR / name).read_text().lower(), name


def test_delete_codes_are_kept_only_as_hashes() -> None:
    assert shares.code_hash(" ABC ") == shares.code_hash("abc")
    assert len(shares.code_hash("abc")) == 32
